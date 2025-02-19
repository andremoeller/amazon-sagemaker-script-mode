#     Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#     Licensed under the Apache License, Version 2.0 (the "License").
#     You may not use this file except in compliance with the License.
#     A copy of the License is located at
#
#         https://aws.amazon.com/apache-2-0/
#
#     or in the "license" file accompanying this file. This file is distributed
#     on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
#     express or implied. See the License for the specific language governing
#     permissions and limitations under the License.


import argparse
import json
import logging
import os
import re

import tensorflow as tf
import tensorflow.keras.backend as K
from tensorflow.keras.callbacks import TensorBoard, ModelCheckpoint
from tensorflow.keras.layers import Activation, Conv2D, Dense, Dropout, Flatten, MaxPooling2D, BatchNormalization
from tensorflow.keras.models import Sequential
from tensorflow.keras.optimizers import Adam, SGD, RMSprop
from tensorflow.contrib.opt import MomentumWOptimizer, AdamWOptimizer

logging.getLogger().setLevel(logging.INFO)
tf.logging.set_verbosity(tf.logging.ERROR)
HEIGHT = 32
WIDTH = 32
DEPTH = 3
NUM_CLASSES = 10
NUM_DATA_BATCHES = 5
NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN = 10000 * NUM_DATA_BATCHES
INPUT_TENSOR_NAME = 'inputs_input'  # needs to match the name of the first layer + "_input"

#  copy inference pre/post-processing script so it will be included in the model package
os.system('mkdir /opt/ml/model/code')
os.system('cp inference.py /opt/ml/model/code')
os.system('cp requirements.txt /opt/ml/model/code')

def keras_model_fn(learning_rate, weight_decay, optimizer, momentum, size):

    model = Sequential()
    model.add(Conv2D(32, (3, 3), padding='same', name='inputs', input_shape=(HEIGHT, WIDTH, DEPTH)))
    model.add(BatchNormalization())
    model.add(Activation('relu'))
    model.add(Conv2D(32, (3, 3)))
    model.add(BatchNormalization())
    model.add(Activation('relu'))
    model.add(MaxPooling2D(pool_size=(2, 2)))
    model.add(Dropout(0.2))

    model.add(Conv2D(64, (3, 3), padding='same'))
    model.add(BatchNormalization())
    model.add(Activation('relu'))
    model.add(Conv2D(64, (3, 3)))
    model.add(BatchNormalization())
    model.add(Activation('relu'))
    model.add(MaxPooling2D(pool_size=(2, 2)))
    model.add(Dropout(0.3))

    model.add(Conv2D(128, (3, 3), padding='same'))
    model.add(BatchNormalization())
    model.add(Activation('relu'))
    model.add(Conv2D(128, (3, 3)))
    model.add(BatchNormalization())
    model.add(Activation('relu'))
    model.add(MaxPooling2D(pool_size=(2, 2)))
    model.add(Dropout(0.4))

    model.add(Flatten())
    model.add(Dense(512))
    model.add(Activation('relu'))
    model.add(Dropout(0.5))
    model.add(Dense(NUM_CLASSES))
    model.add(Activation('softmax'))

    if optimizer.lower() == 'sgd':
        opt = SGD(lr=learning_rate * size, decay=weight_decay, momentum=momentum)
    elif optimizer.lower() == 'rmsprop':
        opt = RMSprop(lr=learning_rate * size, decay=weight_decay)
    else:
        opt = Adam(lr=learning_rate * size, decay=weight_decay)

    model.compile(loss='categorical_crossentropy',
                  optimizer=opt,
                  metrics=['accuracy'])
    return model


class CustomTensorBoardCallback(TensorBoard):
    def on_batch_end(self, batch, logs=None):
        pass


def get_filenames(channel_name, channel):
    if channel_name in ['train', 'validation', 'eval']:
        return [os.path.join(channel, channel_name + '.tfrecords')]
    else:
        raise ValueError('Invalid data subset "%s"' % channel_name)


def train_input_fn():
    return _input(args.epochs, args.batch_size, args.train, 'train')


def eval_input_fn():
    return _input(args.epochs, args.batch_size, args.eval, 'eval')


def validation_input_fn():
    return _input(args.epochs, args.batch_size, args.validation, 'validation')


def _input(epochs, batch_size, channel, channel_name):
    mode = args.data_config[channel_name]['TrainingInputMode']
    """Uses the tf.data input pipeline for CIFAR-10 dataset.
    Args:
        mode: Standard names for model modes (tf.estimators.ModeKeys).
        batch_size: The number of samples per batch of input requested.
    """
    filenames = get_filenames(channel_name, channel)
    # Repeat infinitely.
    logging.info("Running {} in {} mode".format(channel_name, mode))
    if mode == 'Pipe':
        from sagemaker_tensorflow import PipeModeDataset
        dataset = PipeModeDataset(channel=channel_name, record_format='TFRecord')
    else:
        dataset = tf.data.TFRecordDataset(filenames)

    dataset = dataset.repeat(epochs)
    dataset = dataset.prefetch(10)

    # Parse records.
    dataset = dataset.map(
        _dataset_parser, num_parallel_calls=10)

    # Potentially shuffle records.
    if channel_name == 'train':
        # Ensure that the capacity is sufficiently large to provide good random
        # shuffling.
        buffer_size = int(NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN * 0.4) + 3 * batch_size
        dataset = dataset.shuffle(buffer_size=buffer_size)

    # Batch it up.
    dataset = dataset.batch(batch_size, drop_remainder=True)
    iterator = dataset.make_one_shot_iterator()
    image_batch, label_batch = iterator.get_next()

    return {INPUT_TENSOR_NAME: image_batch}, label_batch


def _train_preprocess_fn(image):
    """Preprocess a single training image of layout [height, width, depth]."""
    # Resize the image to add four extra pixels on each side.
    image = tf.image.resize_image_with_crop_or_pad(image, HEIGHT + 8, WIDTH + 8)

    # Randomly crop a [HEIGHT, WIDTH] section of the image.
    image = tf.random_crop(image, [HEIGHT, WIDTH, DEPTH])

    # Randomly flip the image horizontally.
    image = tf.image.random_flip_left_right(image)

    return image


def _dataset_parser(value):
    """Parse a CIFAR-10 record from value."""
    featdef = {
        'image': tf.FixedLenFeature([], tf.string),
        'label': tf.FixedLenFeature([], tf.int64),
    }

    example = tf.parse_single_example(value, featdef)
    image = tf.decode_raw(example['image'], tf.uint8)
    image.set_shape([DEPTH * HEIGHT * WIDTH])

    # Reshape from [depth * height * width] to [depth, height, width].
    image = tf.cast(
        tf.transpose(tf.reshape(image, [DEPTH, HEIGHT, WIDTH]), [1, 2, 0]),
        tf.float32)
    label = tf.cast(example['label'], tf.int32)
    image = _train_preprocess_fn(image)
    return image, tf.one_hot(label, NUM_CLASSES)


def save_model(model, output):

    # create a TensorFlow SavedModel for deployment to a SageMaker endpoint with TensorFlow Serving
    tf.contrib.saved_model.save_keras_model(model, args.model_dir)
    logging.info("Model successfully saved at: {}".format(output))
    return


def main(args):

    if 'sourcedir.tar.gz' in args.tensorboard_dir:
        tensorboard_dir = re.sub('source/sourcedir.tar.gz', 'model', args.tensorboard_dir)
    else:
        tensorboard_dir = args.tensorboard_dir

    logging.info("Writing TensorBoard logs to {}".format(tensorboard_dir))

    logging.info("getting data")
    train_dataset = train_input_fn()
    eval_dataset = eval_input_fn()
    validation_dataset = validation_input_fn()

    logging.info("configuring model")
    logging.info("Hosts: "+os.environ.get('SM_HOSTS'))

    size = len(args.hosts)

    #Deal with this
    model = keras_model_fn(args.learning_rate, args.weight_decay, args.optimizer, args.momentum, size)
    callbacks = []
    if args.current_host == args.hosts[0]:
        callbacks.append(ModelCheckpoint(args.output_dir + '/checkpoint-{epoch}.h5'))
        callbacks.append(CustomTensorBoardCallback(log_dir=tensorboard_dir))

    logging.info("Starting training")

    model.fit(x=train_dataset[0], y=train_dataset[1],
              steps_per_epoch=(num_examples_per_epoch('train') // args.batch_size) // size,
              epochs=args.epochs, validation_data=validation_dataset,
              validation_steps=(num_examples_per_epoch('validation') // args.batch_size) // size, callbacks=callbacks)

    score = model.evaluate(eval_dataset[0], eval_dataset[1], steps=num_examples_per_epoch('eval') // args.batch_size,
                           verbose=0)

    logging.info('Test loss:{}'.format(score[0]))
    logging.info('Test accuracy:{}'.format(score[1]))

    # PS: Save model only on worker 0
    if args.current_host == args.hosts[0]:
        save_model(model, args.model_dir)


def num_examples_per_epoch(subset='train'):
    if subset == 'train':
        return 40000
    elif subset == 'validation':
        return 10000
    elif subset == 'eval':
        return 10000
    else:
        raise ValueError('Invalid data subset "%s"' % subset)


if __name__ == '__main__':

    parser = argparse.ArgumentParser()

    parser.add_argument(
        '--hosts',
        type=list,
        default=json.loads(os.environ.get('SM_HOSTS')))
    parser.add_argument(
        '--current-host',
        type=str,
        default=os.environ.get('SM_CURRENT_HOST'))
    parser.add_argument(
        '--train',
        type=str,
        required=False,
        default=os.environ.get('SM_CHANNEL_TRAIN'),
        help='The directory where the CIFAR-10 input data is stored.')
    parser.add_argument(
        '--validation',
        type=str,
        required=False,
        default=os.environ.get('SM_CHANNEL_VALIDATION'),
        help='The directory where the CIFAR-10 input data is stored.')
    parser.add_argument(
        '--eval',
        type=str,
        required=False,
        default=os.environ.get('SM_CHANNEL_EVAL'),
        help='The directory where the CIFAR-10 input data is stored.')
    parser.add_argument(
        '--model_dir',
        type=str,
        required=True,
        help='The directory where the model will be stored.')
    parser.add_argument(
        '--model_output_dir',
        type=str,
        default=os.environ.get('SM_MODEL_DIR'))
    parser.add_argument(
        '--output-dir',
        type=str,
        default=os.environ.get('SM_OUTPUT_DIR'))
    parser.add_argument(
        '--tensorboard-dir',
        type=str,
        default=os.environ.get('SM_MODULE_DIR'))
    parser.add_argument(
        '--weight-decay',
        type=float,
        default=2e-4,
        help='Weight decay for convolutions.')
    parser.add_argument(
        '--learning-rate',
        type=float,
        default=0.001,
        help="""\
        This is the inital learning rate value. The learning rate will decrease
        during training. For more details check the model_fn implementation in
        this file.\
        """)
    parser.add_argument(
        '--epochs',
        type=int,
        default=10,
        help='The number of steps to use for training.')
    parser.add_argument(
        '--batch-size',
        type=int,
        default=128,
        help='Batch size for training.')
    parser.add_argument(
        '--data-config',
        type=json.loads,
        default=os.environ.get('SM_INPUT_DATA_CONFIG')
    )
    parser.add_argument(
        '--fw-params',
        type=json.loads,
        default=os.environ.get('SM_FRAMEWORK_PARAMS')
    )
    parser.add_argument(
        '--optimizer',
        type=str,
        default='adam'
    )
    parser.add_argument(
        '--momentum',
        type=float,
        default='0.9'
    )
    args = parser.parse_args()

    main(args)
