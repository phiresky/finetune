import itertools
import logging
import sys

from abc import ABCMeta, abstractmethod

import numpy as np
import tensorflow as tf
from tensorflow.python.data import Dataset

from finetune.config import PAD_TOKEN
from finetune.encoding import TextEncoder, ArrayEncodedOutput, EncodedOutput
from finetune.imbalance import compute_class_weights


class BasePipeline(metaclass=ABCMeta):
    def __init__(self, config):
        self.config = config
        self.label_encoder = None
        self.encoder = TextEncoder()
        self.target_dim = None
        self.pad_idx_ = None

    @abstractmethod
    def _target_encoder(self):
        # Overridden by subclass to produce the right target encoding for a given target model.
        raise NotImplementedError

    def feed_shape_type_def(self):
        TS = tf.TensorShape
        return ({"tokens": tf.int32, "mask": tf.float32}, tf.float32), (
            {"tokens": TS([self.config.max_length, 2]), "mask": TS([self.config.max_length])}, TS([self.target_dim]))

    def _array_format(self, encoded_output, pad_token=PAD_TOKEN):
        """
        Returns numpy array of token idxs and corresponding mask
        Returned `x` array contains two channels:
            0: byte-pair encoding embedding
            1: positional embedding
        """
        seq_length = len(encoded_output.token_ids)
        x = np.zeros((self.config.max_length, 2), dtype=np.int32)
        mask = np.zeros((self.config.max_length), dtype=np.float32)

        if encoded_output.labels is not None:
            labels_arr = np.empty((self.config.max_length), dtype='object')
            labels_arr.fill(pad_token)
        else:
            labels_arr = None

        # BPE embedding
        x[:seq_length, 0] = encoded_output.token_ids
        # masking: value of 1 means "consider this in cross-entropy LM loss"
        mask[1:seq_length] = 1
        if encoded_output.labels:
            labels_arr[:seq_length] = encoded_output.labels
        # positional_embeddings
        x[:, 1] = np.arange(self.encoder.vocab_size, self.encoder.vocab_size + self.config.max_length)

        return ArrayEncodedOutput(
            token_ids=x,
            tokens=encoded_output.tokens,
            labels=labels_arr,
            char_locs=encoded_output.char_locs,
            mask=mask,
        )

    def text_to_tokens_mask(self, X, Y=None):
        out_gen = self._text_to_ids(X)
        for out in out_gen:
            feats = {"tokens": out.token_ids, "mask": out.mask}
            if Y is None:
                yield feats
            else:
                yield feats, self.label_encoder.transform([Y])[0]

    def _post_data_initialization(self, Y):
        self.label_encoder = self._target_encoder()
        if not callable(Y):
            Y_fit = Y
            self.config.dataset_size = len(Y)
            self.label_encoder.fit_transform(Y)
        else:
            try:
                self.config.dataset_size = len(Y())
            except TypeError:
                logging.warning(
                    "Generator input function does not have a length, falling back to default in config of {}".format(
                        self.config.dataset_size))
            Y_fit = list(itertools.islice(Y(), 100))  # TODO find a more principled way to do this?
            self.label_encoder.fit_transform(Y_fit)

        target_dim = self.label_encoder.target_dim
        self.lm_loss_coef = self.config.lm_loss_coef if target_dim is not None else 1.0
        if target_dim != self.target_dim and self.target_dim is None:
            self.rebuild = True
        self.target_dim = target_dim

        if Y_fit is not None:
            self.config.class_weights = compute_class_weights(class_weights=self.config.class_weights, Y=Y_fit)

    def _dataset_with_targets(self, Xs, Y):
        if not callable(Xs):
            dataset = lambda: zip(Xs, Y)

        else:
            dataset = lambda: zip(Xs(), Y())  # encode one sample at a time.

        dataset_encoded = lambda: itertools.chain.from_iterable(
            map(lambda xy: self.text_to_tokens_mask(*xy), dataset()))
        return Dataset.from_generator(dataset_encoded, *self.feed_shape_type_def())

    def _dataset_without_targets(self, Xs):
        if not callable(Xs):
            Xs_fn = lambda: Xs
        else:
            Xs_fn = Xs

        dataset_encoded = lambda: itertools.chain.from_iterable(map(self.text_to_tokens_mask, Xs_fn()))
        types, shapes = self.feed_shape_type_def()
        return Dataset.from_generator(dataset_encoded, types[0], shapes[0])  # 0s cut out the targets

    def _get_train_input_fns(self, Xs, Y=None, batch_size=None, val_size=None):
        batch_size = batch_size or self.config.batch_size

        shuffle_buffer_size = 100
        val_size = val_size or 0
        prefetch_buffer = 2  # breaks the pipeline to allow concurrency
        if Y is not None:
            self._post_data_initialization(Y)
            dataset = lambda: self._dataset_with_targets(Xs, Y)
        else:
            dataset = lambda: self._dataset_without_targets(Xs)

        val_dataset = lambda: dataset().shuffle(shuffle_buffer_size, seed=self.config.seed).take(
            val_size).batch(batch_size).prefetch(prefetch_buffer)
        train_dataset = lambda: dataset().shuffle(shuffle_buffer_size, seed=self.config.seed).skip(
            val_size).batch(batch_size).repeat(self.config.n_epochs).prefetch(prefetch_buffer)

        return val_dataset, train_dataset

    def _get_predict_input_fn(self, Xs, batch_size=None):
        batch_size = batch_size or self.config.batch_size
        prefetch_buffer = 2  # breaks the pipeline to allow concurrency
        tf_dataset = lambda: self._dataset_without_targets(Xs)
        return lambda: tf_dataset().batch(batch_size).prefetch(prefetch_buffer)

    @property
    def pad_idx(self):
        if self.pad_idx_ is None:
            self.pad_idx_ = list(self.label_encoder.classes_).index(self.config.pad_token)
        return self.pad_idx_

    def _format_for_encoding(self, X):
        """
        Most subclasses take in inputs as:
            List (batch) of list (docs)

        Encode_multi_input expect the following format:
            List (batch) of list (docs) of list (subseqs) of text

        This method is responsible for standardizing inputs to the above format
        """
        return [[X]]

    def _text_to_ids(self, Xs, Y=None, pad_token=PAD_TOKEN):
        Xs = self._format_for_encoding(Xs)
        if self.config.chunk_long_sequences and len(Xs) == 1:
            # can only chunk single sequence inputs
            chunk_size = self.config.max_length - 2
            step_size = chunk_size // 3
            encoded = self.encoder.encode_multi_input(
                Xs,
                Y=Y,
                max_length=sys.maxsize,
                pad_token=pad_token
            )
            length = len(encoded.token_ids)
            print("length is ", length)
            starts = list(range(0, length, step_size))
            for start in starts:
                d = dict()
                end = start + chunk_size
                for field in EncodedOutput._fields:
                    field_value = getattr(encoded, field)
                    if field_value is not None:
                        print("field value", field_value)
                        d[field] = field_value[start:end]
                yield self._array_format(EncodedOutput(**d), pad_token=pad_token)
        else:
            encoder_out = self.encoder.encode_multi_input(
                Xs,
                Y=Y,
                max_length=self.config.max_length,
                pad_token=pad_token
            )

            yield self._array_format(encoder_out, pad_token=pad_token)