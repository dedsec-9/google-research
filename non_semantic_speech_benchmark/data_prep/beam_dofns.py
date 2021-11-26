# coding=utf-8
# Copyright 2021 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Apache Beam DoFns for data prep.
"""

from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple, Union

from absl import logging
import apache_beam as beam
import librosa
import numpy as np
import tensorflow as tf
import tensorflow_hub as hub

from non_semantic_speech_benchmark.data_prep import data_prep_utils as utils


@beam.typehints.with_input_types(Tuple[str, tf.train.Example])
@beam.typehints.with_output_types(Tuple[str, np.ndarray])
class ComputeEmbeddingMapFn(beam.DoFn):
  """Computes an embedding (key, tf.Example) from audio (key, tf.Example)."""

  def __init__(
      self,
      name,
      module,
      output_key,
      audio_key,
      sample_rate_key,
      sample_rate,
      average_over_time,
      feature_fn = None,
      normalize_to_pm_one = True,
      model_input_min_length = None,
      target_sample_rate = 16000,
      module_call_fn = utils.samples_to_embedding_tfhub,
      setup_fn = hub.load):
    self._name = name
    # If TFLite should be used, `module` should point to a flatbuffer model.
    self._module = module
    # For TFLite, `output_key` is the index of the embedding output from TFLite
    # model (Usually 0).
    self._output_key = output_key
    self._audio_key = audio_key
    self._sample_rate_key = sample_rate_key
    self._sample_rate = sample_rate
    self._average_over_time = average_over_time
    self._feature_fn = feature_fn
    self._normalize_to_pm_one = normalize_to_pm_one
    self._model_input_min_length = model_input_min_length
    self._target_sample_rate = target_sample_rate
    self._module_call_fn = module_call_fn
    self._setup_fn = setup_fn

    # Only one of `sample_rate_key` and `sample_rate` should be not None.
    if not (self._sample_rate_key is None) ^ (self._sample_rate is None):
      raise ValueError('Must have exactly one sample_rate_key or sample_rate: '
                       f'{self._sample_rate_key} vs {self._sample_rate}')

  def setup(self):
    self.post_setup_module = self._setup_fn(self._module)

  def _read_audio_from_tfexample(
      self,
      ex,
      k,
      normalize_to_pm_one = True):
    """Reads the audio samples from a tf.Example, and assert input sanity."""
    if self._audio_key not in ex.features.feature:
      raise ValueError(f'Audio key `{self._audio_key}` not found: '
                       f'{list(ex.features.feature.keys())}')
    audio = utils.tfexample_audio_to_npfloat32(ex, self._audio_key,
                                               normalize_to_pm_one, k)
    assert audio.ndim == 1, audio.ndim
    if audio.size == 0:
      raise ValueError(f'No audio found: {self._audio_key}, {audio.size} {k}')
    beam.metrics.Metrics.distribution(
        'computed-embedding-audio', 'length').update(audio.size)

    return audio

  def _read_sample_rate_from_tfexample(self, ex):
    """Reads the sample rate from a tf.Example."""
    if self._sample_rate_key:
      logging.info('read_sample_rate_from_tfexample: has `_sample_rate_key`.')
      if self._sample_rate_key not in ex.features.feature:
        raise ValueError(f'Sample rate key not found: {self._sample_rate_key}')
      sr_feat = ex.features.feature[self._sample_rate_key]
      # Use `sample_rate` in `float_list` or `int64_list`. Either way, convert
      # to an integer for downstream use.
      if not len(sr_feat.float_list.value) ^ len(sr_feat.int64_list.value):
        raise ValueError(
            f'Expected exactly one of `float_list` and `int64_list`: {sr_feat}')
      if sr_feat.float_list.value:
        sample_rate = int(sr_feat.float_list.value[0])
      else:
        sample_rate = sr_feat.int64_list.value[0]
    else:
      logging.info('read_sample_rate_from_tfexample: Default sample rate.')
      if not self._sample_rate:
        raise ValueError('If `sample_rate_key` not provided, must provide '
                         '`sample_rate`.')
      sample_rate = self._sample_rate

    logging.info('read_sample_rate_from_tfexample: sr: %s', sample_rate)
    return sample_rate

  def resample(self, audio, sample_rate,
               target_sr):
    """Resample audio to target."""
    return librosa.core.resample(
        audio, orig_sr=sample_rate, target_sr=target_sr, res_type='kaiser_best')

  def read_and_preprocess_audio(self, k,
                                ex):
    # Read the input example audio and assert input format sanity.
    audio = self._read_audio_from_tfexample(
        ex, k, normalize_to_pm_one=self._normalize_to_pm_one)

    # Read the sample rate, if a key to do so has been provided.
    sample_rate = self._read_sample_rate_from_tfexample(ex)

    # Resample, if necessary.
    if sample_rate != self._target_sample_rate:
      audio = self.resample(
          audio, sample_rate, target_sr=self._target_sample_rate)
      sample_rate = self._target_sample_rate

    # Convert audio to features, if required.
    model_input = self._audio_to_features(audio, sample_rate)

    logging.info('read_and_preprocess_audio: %s / %s / %s / %s',
                 model_input.shape, len(audio), sample_rate, self._name)

    return model_input, sample_rate

  def _audio_to_features(self, audio,
                         sample_rate):
    """Convert audio to features, if required."""
    if self._feature_fn:
      model_input = self._feature_fn(audio, sample_rate)
      if not isinstance(model_input, np.ndarray):
        raise ValueError(f'Expected ndarray, got {type(model_input)}')
      if model_input.dtype != np.float32:
        raise ValueError(f'Should be float32, was: {model_input.dtype}')
    else:
      model_input = audio
      if self._model_input_min_length and model_input.size < self._model_input_min_length:
        delta = self._model_input_min_length - model_input.size
        model_input = np.pad(model_input, [0, delta], mode='symmetric')
    logging.info('`model_input` shape is: %s, %s', model_input.shape,
                 self._model_input_min_length)

    return model_input

  def process(
      self, k_v):
    k, ex = k_v

    # Read audio from tf.Example, get the sample rate, resample if necessary,
    # and convert to model inputs (if necessary).
    model_input, sample_rate = self.read_and_preprocess_audio(k, ex)

    # Calculate the 2D embedding.
    embedding_2d = self._module_call_fn(
        model_input, sample_rate, self.post_setup_module, self._output_key,
        self._name)
    if not isinstance(embedding_2d, np.ndarray):
      raise ValueError(f'`embedding_2d` wrong type: {type(embedding_2d)}')
    if embedding_2d.ndim != 2:
      raise ValueError(f'`embedding_2d` wrong dims: {embedding_2d.ndim}')
    if embedding_2d.dtype != np.float32:
      raise ValueError(f'`embedding_2d` wrong type: {embedding_2d.dtype}')
    logging.info('[%s] `embedding_2d` shape: %s', self._name,
                 embedding_2d.shape)
    beam.metrics.Metrics.counter('computed-embedding', self._name).inc()
    beam.metrics.Metrics.distribution(f'computed-embedding-{self._name}',
                                      'length').update(embedding_2d.shape[0])

    # Average over time, if required.
    if self._average_over_time:
      embedding = np.mean(embedding_2d, axis=0, keepdims=True)
    else:
      embedding = embedding_2d

    yield (k, embedding)


@beam.typehints.with_input_types(Tuple[str, tf.train.Example])
@beam.typehints.with_output_types(
    Tuple[str, tf.train.Example, Dict[str, np.ndarray]])
class ComputeMultipleEmbeddingsFromSingleModel(ComputeEmbeddingMapFn):
  """Computes an embedding (key, tf.Example) from audio (key, tf.Example)."""

  def __init__(self,
               *args,
               embedding_names,
               chunk_len = None,
               embedding_length = None,
               # Change the default `module_call_fn`.
               module_call_fn = utils.samples_to_embedding_tfhub_w2v2,
               **kwargs):
    super(ComputeMultipleEmbeddingsFromSingleModel, self).__init__(
        *args, **kwargs)
    self._chunk_len = chunk_len
    self._output_keys = self._output_key
    self._embedding_names = embedding_names
    self._embedding_len = embedding_length
    self._module_call_fn = module_call_fn
    assert isinstance(self._output_keys, (tuple, list))

  def tfex_to_chunked_audio(self, k,
                            ex):

    # Read audio from tf.Example, get the sample rate, resample if necessary,
    # and convert to model inputs (if necessary).
    model_input, sample_rate = self.read_and_preprocess_audio(k, ex)

    # Do some chunking.
    if self._chunk_len:
      logging.info('Chunk len: %s', self._chunk_len)
      if model_input.shape[0] >= self._chunk_len:
        model_input = utils.get_chunked_audio_fn(model_input, self._chunk_len)
      logging.info('model_input after chunking: ')

    return model_input, sample_rate

  def process(self, k_v):
    k, ex = k_v

    # Get dictionary of chunked audio.
    model_input, _ = self.tfex_to_chunked_audio(k, ex)

    # Calculate the 3D embeddings.
    if model_input.ndim == 1:
      model_input = np.expand_dims(model_input, axis=0)
    tf_out = self._module_call_fn(model_input, self.post_setup_module)

    out_dict = {}
    for name, output_key in zip(self._embedding_names, self._output_keys):
      assert isinstance(name, str)
      if output_key not in tf_out:
        raise ValueError(
            f'Output key not recognized: {output_key} vs {tf_out.keys()}')
      cur_emb = np.array(tf_out[output_key])
      if cur_emb.ndim != 3:  #  (chunk size, time, embedding dim)
        raise ValueError(f'Wrong output dim size: {cur_emb.ndim}')
      if cur_emb.dtype != np.float32:
        raise ValueError(f'Wrong dtype: {cur_emb.dtype}')

      embedding_2d = np.mean(cur_emb, axis=0, keepdims=False)
      embedding_2d = np.mean(embedding_2d, axis=0, keepdims=True)
      assert isinstance(embedding_2d, np.ndarray)
      assert embedding_2d.ndim == 2, embedding_2d.shape
      assert embedding_2d.dtype == np.float32
      if self._average_over_time and embedding_2d.shape[0] != 1:
        raise ValueError(f'Wrong batch dim: {embedding_2d.shape[0]} vs {1}')
      if self._embedding_len and embedding_2d.shape[1] != self._embedding_len:
        raise ValueError(f'Wrong output dim: {embedding_2d.shape[1]}')
      out_dict[name] = embedding_2d
    yield (k, ex, out_dict)


@beam.typehints.with_input_types(Tuple[str, tf.train.Example])
@beam.typehints.with_output_types(Tuple[
    str, np.ndarray, Optional[bytes], Optional[bytes],
    Dict[str, np.ndarray]])
class ChunkAudioAndComputeEmbeddings(ComputeMultipleEmbeddingsFromSingleModel):
  """Computes an embedding (key, tf.Example) from audio (key, tf.Example)."""

  def __init__(
      self,
      *args,
      label_key=None,
      speaker_id_key=None,
      # Change the default `module_call_fn`.
      module_call_fn = utils.samples_to_embedding_tfhub_w2v2,
      compute_embeddings_on_chunked_audio = True,
      **kwargs):
    super(ChunkAudioAndComputeEmbeddings, self).__init__(*args, **kwargs)
    self._label_key = label_key
    self._speaker_id_key = speaker_id_key
    self._module_call_fn = module_call_fn
    self._compute_embeddings_on_chunked_audio = compute_embeddings_on_chunked_audio
    logging.info('chunk_len: %s', self._chunk_len)
    logging.info('label_key: %s', self._label_key)
    logging.info('speaker_id_key: %s', self._speaker_id_key)

  def process(
      self, k_v):
    k, ex = k_v

    # Get dictionary of chunked audio.
    chnkd_audio, _ = self.tfex_to_chunked_audio(k, ex)
    if chnkd_audio.ndim == 1:
      chnkd_audio = np.expand_dims(chnkd_audio, axis=0)

    # Calculate the 3D embeddings.
    if self._compute_embeddings_on_chunked_audio:
      model_input = chnkd_audio
    else:
      model_input, _ = self.read_and_preprocess_audio(k, ex)
    tf_out = self._module_call_fn(chnkd_audio, self.post_setup_module)

    cur_embs = [np.array(tf_out[okey]) for okey in self._output_key]
    for emb in cur_embs:
      if emb.ndim != 3:  # (chunk, time, emb dim)
        raise ValueError(f'Wrong output dims: {emb.shape}')
    if self._average_over_time:
      embedding_3ds = [np.mean(x, axis=1, keepdims=True) for x in cur_embs]
    else:
      embedding_3ds = cur_embs

    for x in embedding_3ds:
      assert isinstance(x, np.ndarray)
      assert x.ndim == 3
      assert x.dtype == np.float32
      assert x.shape[0] == chnkd_audio.shape[0], (x.shape, chnkd_audio.shape)
      if self._embedding_len:
        assert x.shape[2] == self._embedding_len, x.shape
      if self._average_over_time:
        assert x.shape[1] == 1, x.shape

    # Get the label, if a key to do so has been provided.
    label = _get_label(self._label_key, ex) if self._label_key else None
    logging.info('`label` is: %s', label)
    if label:
      assert isinstance(label, bytes)

    # Get the speaker ID, if a label has been provided.
    speaker_id = (
        _get_speaker_id(self._speaker_id_key, ex)
        if self._speaker_id_key else None)
    logging.info('`speaker_id` is: %s', speaker_id)
    if speaker_id:
      assert isinstance(speaker_id, bytes)

    for i in range(chnkd_audio.shape[0]):
      cur_k = f'{k}_{i}'
      cur_audio = np.array(chnkd_audio[i])
      out_dict = {
          name: x[i] for name, x in zip(self._embedding_names, embedding_3ds)}
      yield (cur_k, cur_audio, label, speaker_id, out_dict)


def _get_label(label_key, ex):
  """Gets a label."""
  if label_key not in ex.features.feature:
    raise ValueError(
        f'label key not found: {label_key} vs {ex.features.feature}')
  lbl_feat = ex.features.feature[label_key]
  if lbl_feat.int64_list.value:
    label = str(lbl_feat.int64_list.value[0]).encode('utf-8')
  else:
    assert lbl_feat.bytes_list.value
    label = lbl_feat.bytes_list.value[0]
  assert label
  assert isinstance(label, bytes)
  return label


def _get_speaker_id(speaker_id_key, ex):
  """Get speakerID from tf.Example."""
  if speaker_id_key not in ex.features.feature:
    raise ValueError(f'speaker_id key not found: {speaker_id_key} vs '
                     f'{ex.features.feature}')
  speaker_id = ex.features.feature[speaker_id_key].bytes_list.value[0]
  assert speaker_id
  return speaker_id
