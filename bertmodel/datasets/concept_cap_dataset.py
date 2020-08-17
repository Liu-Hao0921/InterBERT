import copy
import json
import logging
import os
import random

import lmdb
import numpy as np
import tensorpack.dataflow as td

import torch
from torch.utils.data import Dataset
from torch.utils.data.sampler import Sampler
import torch.distributed as dist
import sys
import pdb

import scipy
from scipy.spatial import distance

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
    datefmt="%m/%d/%Y %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def iou_numpy(boxes_1, boxes_2, width, height):
    """
    boxes_1: (N_1, 5) ndarray of float (normalized)
    boxes_2: (N_2, 5) ndarray of float (normalized)
    overlaps: (N_1, N_2) ndarray of iou overlap between every pair of boxes
    """
    N_1 = boxes_1.shape[0]
    N_2 = boxes_2.shape[0]

    boxes_1_area = ((boxes_1[:,2] - boxes_1[:,0] + 1.0 / width) *
                (boxes_1[:,3] - boxes_1[:,1] + 1.0 / height)).reshape(1, N_1)

    boxes_2_area = ((boxes_2[:,2] - boxes_2[:,0] + 1.0 / width) *
                (boxes_2[:,3] - boxes_2[:,1] + 1.0 / height)).reshape(N_2, 1)

    boxes_1_expand = np.tile(boxes_1.reshape(N_1, 1, 5), [1, N_2, 1])
    boxes_2_expand = np.tile(boxes_2.reshape(1, N_2, 5), [N_1, 1, 1])

    iw = np.where(boxes_1_expand[:,:,2] < boxes_2_expand[:,:,2], boxes_1_expand[:,:,2], boxes_2_expand[:,:,2]) - \
        np.where(boxes_1_expand[:,:,0] > boxes_2_expand[:,:,0], boxes_1_expand[:,:,0], boxes_2_expand[:,:,0]) + 1.0 / width
    iw[iw < 0] = 0

    ih = np.where(boxes_1_expand[:,:,3] < boxes_2_expand[:,:,3], boxes_1_expand[:,:,3], boxes_2_expand[:,:,3]) - \
        np.where(boxes_1_expand[:,:,1] > boxes_2_expand[:,:,1], boxes_1_expand[:,:,1], boxes_2_expand[:,:,1]) + 1.0 / height
    ih[ih < 0] = 0

    ua = boxes_1_area + boxes_2_area - (iw * ih)
    overlaps = iw * ih / ua

    return overlaps


class InputExample(object):
    """A single training/test example for the language model."""

    def __init__(
        self, image_feat=None, image_target=None, caption=None, is_next=None, lm_labels=None, image_loc=None, num_boxes=None, image_w=None, image_h=None
    ):
        """Constructs a InputExample.
        Args:
            guid: Unique id for the example.
            tokens_a: string. The untokenized text of the first sequence. For single
            sequence tasks, only this sequence must be specified.
            tokens_b: (Optional) string. The untokenized text of the second sequence.
            Only must be specified for sequence pair tasks.
            label: (Optional) string. The label of the example. This should be
            specified for train and dev examples, but not for test examples.
        """
        self.image_feat = image_feat
        self.caption = caption
        self.is_next = is_next  # nextSentence
        self.lm_labels = lm_labels  # masked words for language model
        self.image_loc = image_loc
        self.image_target = image_target
        self.num_boxes = num_boxes
        self.image_w = image_w
        self.image_h = image_h

class InputFeatures(object):
    """A single set of features of data."""

    def __init__(
        self,
        input_ids=None,
        input_mask=None,
        segment_ids=None,
        is_next=None,
        lm_label_ids=None,
        image_feat=None,
        image_target=None,
        image_loc=None,
        image_label=None,
        image_mask=None,
        multimodal_mask=None
    ):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.is_next = is_next
        self.lm_label_ids = lm_label_ids
        self.image_feat = image_feat
        self.image_loc = image_loc
        self.image_label = image_label
        self.image_target = image_target
        self.image_mask = image_mask
        self.multimodal_mask = multimodal_mask

class ConceptCapLoaderTrain(object):
    """
    Data loader. Combines a dataset and a sampler, and provides
    single- or multi-process iterators over the dataset.
    Arguments:
        mode (str, required): mode of dataset to operate in, one of ['train', 'val']
        batch_size (int, optional): how many samples per batch to load
            (default: 1).
        shuffle (bool, optional): set to ``True`` to have the data reshuffled
            at every epoch (default: False).
        num_workers (int, optional): how many subprocesses to use for data
            loading. 0 means that the data will be loaded in the main process
            (default: 0)
        cache (int, optional): cache size to use when loading data,
        drop_last (bool, optional): set to ``True`` to drop the last incomplete batch,
            if the dataset size is not divisible by the batch size. If ``False`` and
            the size of dataset is not divisible by the batch size, then the last batch
            will be smaller. (default: False)
        cuda (bool, optional): set to ``True`` and the PyTorch tensors will get preloaded
            to the GPU for you (necessary because this lets us to uint8 conversion on the 
            GPU, which is faster).
    """

    def __init__(
        self,
        corpus_path,
        tokenizer,
        seq_len,
        encoding="utf-8",
        predict_feature=False,
        batch_size=512,
        shuffle=False,
        num_workers=25,
        cache=10000,
        drop_last=False,
        cuda=False,
        distributed=False,
        visualization=False,
        span_mask=False,
        cond_mask=False,
        region_len=36
    ):

        if dist.is_available() and distributed:
            rank = dist.get_rank()
            lmdb_file = os.path.join(corpus_path, "training_feat_part_" + str(rank) + ".lmdb")
        else:
            lmdb_file = os.path.join(corpus_path, "training_feat_all.lmdb")
            
        caption_path = os.path.join(corpus_path, "caption_train.json")
        
        print("Loading from %s" % lmdb_file)

        os.listdir(corpus_path)

        ds = td.LMDBSerializer.load(lmdb_file, shuffle=False)
        self.num_dataset = len(ds)
        
        self.cond_mask = cond_mask

        preprocess_function = BertPreprocessBatch(
            caption_path,
            tokenizer,
            seq_len,
            region_len,
            self.num_dataset,
            encoding="utf-8",
            predict_feature=predict_feature,
            span_mask=span_mask,
            cond_mask=cond_mask
        )

        # ds = td.LocallyShuffleData(ds, cache)
        ds = td.PrefetchData(ds, 5000, 1)
        ds = td.MapData(ds, preprocess_function)
        # self.ds = td.PrefetchData(ds, 1)
        ds = td.PrefetchDataZMQ(ds, num_workers)
        self.ds = td.BatchData(ds, batch_size)
        # self.ds = ds
        self.ds.reset_state()

        self.batch_size = batch_size
        self.num_workers = num_workers

    def __iter__(self):
        for batch in self.ds.get_data():
            if self.cond_mask:
                batches = [batch[:12], batch[12:]]
                for batch in batches:
                    input_ids, input_mask, segment_ids, lm_label_ids, is_next, image_feat, \
                    image_loc, image_target, image_label, image_mask, multimodal_mask, image_id = batch

                    batch_size = input_ids.shape[0]
                    g_image_feat = np.sum(image_feat, axis=1) / np.sum(image_mask, axis=1, keepdims=True)
                    image_feat = np.concatenate([np.expand_dims(g_image_feat, axis=1), image_feat], axis=1)
                    image_feat = np.array(image_feat, dtype=np.float32)

                    g_image_loc = np.repeat(np.array([[0,0,1,1,1]], dtype=np.float32), batch_size, axis=0)
                    image_loc = np.concatenate([np.expand_dims(g_image_loc, axis=1), image_loc], axis=1)
                    
                    image_loc = np.array(image_loc, dtype=np.float32)
                    g_image_mask = np.repeat(np.array([[1]]), batch_size, axis=0)
                    image_mask = np.concatenate([g_image_mask, image_mask], axis=1)
                    
                    multimodal_mask = np.concatenate([g_image_mask, multimodal_mask], axis=1)

                    # batch = (input_ids, input_mask, segment_ids, lm_label_ids, is_next, image_feat, \
                    # image_loc, image_target, image_label, image_mask, image_id)
                    batch = (input_ids, input_mask, segment_ids, lm_label_ids, is_next, image_feat, \
                        image_loc, image_target, image_label, image_mask, multimodal_mask, image_id)
                    
                    b_tensor = [torch.tensor(data) for data in batch]
                    yield tuple(b_tensor)
            else:
                input_ids, input_mask, segment_ids, lm_label_ids, is_next, image_feat, \
                image_loc, image_target, image_label, image_mask, multimodal_mask, image_id = batch

                batch_size = input_ids.shape[0]
                g_image_feat = np.sum(image_feat, axis=1) / np.sum(image_mask, axis=1, keepdims=True)
                image_feat = np.concatenate([np.expand_dims(g_image_feat, axis=1), image_feat], axis=1)
                image_feat = np.array(image_feat, dtype=np.float32)

                g_image_loc = np.repeat(np.array([[0,0,1,1,1]], dtype=np.float32), batch_size, axis=0)
                image_loc = np.concatenate([np.expand_dims(g_image_loc, axis=1), image_loc], axis=1)
                
                image_loc = np.array(image_loc, dtype=np.float32)
                g_image_mask = np.repeat(np.array([[1]]), batch_size, axis=0)
                image_mask = np.concatenate([g_image_mask, image_mask], axis=1)
                
                multimodal_mask = np.concatenate([g_image_mask, multimodal_mask], axis=1)

                # batch = (input_ids, input_mask, segment_ids, lm_label_ids, is_next, image_feat, \
                # image_loc, image_target, image_label, image_mask, image_id)
                batch = (input_ids, input_mask, segment_ids, lm_label_ids, is_next, image_feat, \
                    image_loc, image_target, image_label, image_mask, multimodal_mask, image_id)
                
                b_tensor = [torch.tensor(data) for data in batch]
                # yield tuple([torch.tensor(data) for data in batch] + [image_id])
                # yield tuple([torch.tensor(data) for data in batch])
                yield tuple(b_tensor)

    def __len__(self):
        return self.ds.size()

class ConceptCapLoaderVal(object):
    """
    Data loader. Combines a dataset and a sampler, and provides
    single- or multi-process iterators over the dataset.
    Arguments:
        mode (str, required): mode of dataset to operate in, one of ['train', 'val']
        batch_size (int, optional): how many samples per batch to load
            (default: 1).
        shuffle (bool, optional): set to ``True`` to have the data reshuffled
            at every epoch (default: False).
        num_workers (int, optional): how many subprocesses to use for data
            loading. 0 means that the data will be loaded in the main process
            (default: 0)
        cache (int, optional): cache size to use when loading data,
        drop_last (bool, optional): set to ``True`` to drop the last incomplete batch,
            if the dataset size is not divisible by the batch size. If ``False`` and
            the size of dataset is not divisible by the batch size, then the last batch
            will be smaller. (default: False)
        cuda (bool, optional): set to ``True`` and the PyTorch tensors will get preloaded
            to the GPU for you (necessary because this lets us to uint8 conversion on the 
            GPU, which is faster).
    """

    def __init__(
        self,
        corpus_path,
        tokenizer,
        seq_len,
        encoding="utf-8",
        predict_feature=False,
        batch_size=512,
        shuffle=False,
        num_workers=25,
        cache=50000,
        drop_last=False,
        cuda=False,
        distributed=False,
        visualization=False,
        span_mask=False,
        cond_mask=False,
        region_len=36
    ):
    
        lmdb_file = os.path.join(corpus_path, "validation_all.lmdb")

        caption_path = os.path.join(corpus_path, "caption_val.json")

        print("Loading from %s" % lmdb_file)

        ds = td.LMDBSerializer.load(lmdb_file, shuffle=False)
        self.num_dataset = len(ds)
        
        self.cond_mask = cond_mask
        
        preprocess_function = BertPreprocessBatch(
            caption_path,
            tokenizer,
            seq_len,
            region_len,
            self.num_dataset,
            encoding="utf-8",
            predict_feature=predict_feature,
            visualization=visualization,
            span_mask=span_mask, 
            cond_mask=cond_mask,
        )

        ds = td.MapData(ds, preprocess_function)
        self.ds = td.BatchData(ds, batch_size)
        self.ds.reset_state()

        self.batch_size = batch_size
        self.num_workers = num_workers

    def __iter__(self):
        for batch in self.ds.get_data():
            if self.cond_mask:
                batches = [batch[:12], batch[12:]]
                for batch in batches:
                    input_ids, input_mask, segment_ids, lm_label_ids, is_next, image_feat, \
                    image_loc, image_target, image_label, image_mask, multimodal_mask, image_id = batch

                    batch_size = input_ids.shape[0]
                    g_image_feat = np.sum(image_feat, axis=1) / np.sum(image_mask, axis=1, keepdims=True)
                    image_feat = np.concatenate([np.expand_dims(g_image_feat, axis=1), image_feat], axis=1)
                    image_feat = np.array(image_feat, dtype=np.float32)

                    g_image_loc = np.repeat(np.array([[0,0,1,1,1]], dtype=np.float32), batch_size, axis=0)
                    image_loc = np.concatenate([np.expand_dims(g_image_loc, axis=1), image_loc], axis=1)
                    
                    image_loc = np.array(image_loc, dtype=np.float32)
                    g_image_mask = np.repeat(np.array([[1]]), batch_size, axis=0)
                    image_mask = np.concatenate([g_image_mask, image_mask], axis=1)
                    
                    multimodal_mask = np.concatenate([g_image_mask, multimodal_mask], axis=1)

                    # batch = (input_ids, input_mask, segment_ids, lm_label_ids, is_next, image_feat, \
                    # image_loc, image_target, image_label, image_mask, image_id)
                    batch = (input_ids, input_mask, segment_ids, lm_label_ids, is_next, image_feat, \
                        image_loc, image_target, image_label, image_mask, multimodal_mask, image_id)
                    
                    b_tensor = [torch.tensor(data) for data in batch]
                    yield tuple(b_tensor)
            else:
                input_ids, input_mask, segment_ids, lm_label_ids, is_next, image_feat, \
                image_loc, image_target, image_label, image_mask, multimodal_mask, image_id = batch

                batch_size = input_ids.shape[0]
                g_image_feat = np.sum(image_feat, axis=1) / np.sum(image_mask, axis=1, keepdims=True)
                image_feat = np.concatenate([np.expand_dims(g_image_feat, axis=1), image_feat], axis=1)
                image_feat = np.array(image_feat, dtype=np.float32)

                g_image_loc = np.repeat(np.array([[0,0,1,1,1]], dtype=np.float32), batch_size, axis=0)
                image_loc = np.concatenate([np.expand_dims(g_image_loc, axis=1), image_loc], axis=1)
                
                image_loc = np.array(image_loc, dtype=np.float32)
                g_image_mask = np.repeat(np.array([[1]]), batch_size, axis=0)
                image_mask = np.concatenate([g_image_mask, image_mask], axis=1)
                
                multimodal_mask = np.concatenate([g_image_mask, multimodal_mask], axis=1)

                # batch = (input_ids, input_mask, segment_ids, lm_label_ids, is_next, image_feat, \
                # image_loc, image_target, image_label, image_mask, image_id)
                batch = (input_ids, input_mask, segment_ids, lm_label_ids, is_next, image_feat, \
                    image_loc, image_target, image_label, image_mask, multimodal_mask, image_id)
                
                b_tensor = [torch.tensor(data) for data in batch]
                # yield tuple([torch.tensor(data) for data in batch] + [image_id])
                # yield tuple([torch.tensor(data) for data in batch])
                yield tuple(b_tensor)

    def __len__(self):
        return self.ds.size()


class BertPreprocessBatch(object):
    def __init__(
        self,
        caption_path,
        tokenizer,
        seq_len,
        region_len, 
        data_size,
        split="Train",
        encoding="utf-8",
        predict_feature=False,
        visualization=False,
        span_mask=False, 
        cond_mask=False
    ):

        self.split = split
        self.seq_len = seq_len
        self.region_len = region_len
        self.tokenizer = tokenizer
        self.predict_feature = predict_feature
        # self.num_caps = data_size
        # self.captions = list(json.load(open(caption_path, 'r')).values())
        self.captions = json.load(open(caption_path, 'r'))
        self.all_pair_ids = list(self.captions.keys())
        self.num_caps = len(self.captions)
        self.visualization = visualization
        self.span_mask = span_mask
        self.cond_mask = cond_mask

    def __call__(self, data):

        image_feature_wp, image_target_wp, image_location_wp, num_boxes, image_h, image_w, image_id, caption = data
        
        image_feature = np.zeros((self.region_len, 2048), dtype=np.float32)
        image_target = np.zeros((self.region_len, 1601), dtype=np.float32)
        image_location = np.zeros((self.region_len, 5), dtype=np.float32)

        num_boxes = int(num_boxes)
        image_feature[:num_boxes] = image_feature_wp
        image_target[:num_boxes] = image_target_wp
        image_location[:num_boxes,:4] = image_location_wp

        image_location[:,4] = (image_location[:,3] - image_location[:,1]) * (image_location[:,2] - image_location[:,0]) / (float(image_w) * float(image_h))
        
        image_location[:,0] = image_location[:,0] / float(image_w)
        image_location[:,1] = image_location[:,1] / float(image_h)
        image_location[:,2] = image_location[:,2] / float(image_w)
        image_location[:,3] = image_location[:,3] / float(image_h)

        if self.predict_feature:
            image_feature = copy.deepcopy(image_feature)
            image_target = copy.deepcopy(image_feature)
        else:
            image_feature = copy.deepcopy(image_feature)
            image_target = copy.deepcopy(image_target)            

        caption, label = self.random_cap(caption, image_id)

        tokens_caption = self.tokenizer.tokenize(caption)
        cur_example = InputExample(
            image_feat=image_feature,
            image_target=image_target,
            caption=tokens_caption,
            is_next=label,
            image_loc=image_location,
            num_boxes=num_boxes,
            image_w=float(image_w),
            image_h=float(image_h)
        )

        # transform sample to features
        if self.cond_mask:
            text_features = self.convert_example_to_features(cur_example, self.seq_len, self.tokenizer, self.region_len, text_cond_mask=True)
            text_tensors = [
                text_features.input_ids,
                text_features.input_mask,
                text_features.segment_ids,
                text_features.lm_label_ids,
                text_features.is_next,
                text_features.image_feat,
                text_features.image_loc,
                text_features.image_target,
                text_features.image_label,
                text_features.image_mask,
                text_features.multimodal_mask,
                int(image_id),
            ]
            img_features = self.convert_example_to_features(cur_example, self.seq_len, self.tokenizer, self.region_len, image_cond_mask=True)
            img_tensors = [
                img_features.input_ids,
                img_features.input_mask,
                img_features.segment_ids,
                img_features.lm_label_ids,
                img_features.is_next,
                img_features.image_feat,
                img_features.image_loc,
                img_features.image_target,
                img_features.image_label,
                img_features.image_mask,
                img_features.multimodal_mask,
                int(image_id),
            ]
            cur_tensors = tuple(text_tensors+img_tensors)
        else:
            cur_features = self.convert_example_to_features(cur_example, self.seq_len, self.tokenizer, self.region_len)
            cur_tensors = (
                cur_features.input_ids,
                cur_features.input_mask,
                cur_features.segment_ids,
                cur_features.lm_label_ids,
                cur_features.is_next,
                cur_features.image_feat,
                cur_features.image_loc,
                cur_features.image_target,
                cur_features.image_label,
                cur_features.image_mask,
                cur_features.multimodal_mask,
                int(image_id),
            )
        return cur_tensors

    def random_cap(self, caption, image_id):
        """
        Get one sample from corpus consisting of two sentences. With prob. 50% these are two subsequent sentences
        from one doc. With 50% the second sentence will be a random one from another doc.
        :param index: int, index of sample.
        :return: (str, str, int), sentence 1, sentence 2, isNextSentence Label
        """

        if self.visualization:
            return caption, 0

        if random.random() > 0.5:
            label = 0
        else:
            caption = self.get_random_caption(image_id)
            label = 1

        return caption, label

    def get_random_caption(self, image_id):
        """
        Get random caption from another document for nextSentence task.
        :return: str, content of one line
        """
        # Similar to original tf repo: This outer loop should rarely go for more than one iteration for large
        # corpora. However, just to be careful, we try to make sure that
        # the random document is not the same as the document we're processing.

        # add the hard negative mining objective here.
        target_image_id = image_id
        while target_image_id[:-1] == image_id[:-1]: # ensure the sampled caption not matches with the image
            rand_doc_idx = random.randint(0, self.num_caps - 1)
            target_image_id = self.all_pair_ids[rand_doc_idx]
        caption = self.captions[target_image_id]


        return caption

    def convert_example_to_features(self, example, max_seq_length, tokenizer, max_region_length, text_cond_mask=False, image_cond_mask=False):
        """
        Convert a raw sample (pair of sentences as tokenized strings) into a proper training sample with
        IDs, LM labels, input_mask, CLS and SEP tokens etc.
        :param example: InputExample, containing sentence input as strings and is_next label
        :param max_seq_length: int, maximum length of sequence.
        :param tokenizer: Tokenizer
        :return: InputFeatures, containing all inputs and labels of one sample as IDs (as used for model training)
        """
        image_feat = example.image_feat
        caption = example.caption
        image_loc = example.image_loc
        image_target = example.image_target
        image_w = example.image_w
        image_h = example.image_h
        num_boxes = int(example.num_boxes)
        self._truncate_seq_pair(caption, max_seq_length - 2)
        
        caption, caption_label = self.random_word(caption, tokenizer, span_mask=self.span_mask, cond_mask=text_cond_mask)
        image_feat, image_loc, image_label = self.random_region(image_feat, image_loc, num_boxes, span_mask=self.span_mask, cond_mask=image_cond_mask, image_w=image_w, image_h=image_h)

        # concatenate lm labels and account for CLS, SEP, SEP
        # lm_label_ids = ([-1] + caption_label + [-1] + image_label + [-1])
        lm_label_ids = [-1] + caption_label + [-1]
        # image_label = ([-1] + image_label)

        # The convention in BERT is:
        # (a) For sequence pairs:
        #  tokens:   [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]
        #  type_ids: 0   0  0    0    0     0       0 0    1  1  1  1   1 1
        # (b) For single sequences:
        #  tokens:   [CLS] the dog is hairy . [SEP]
        #  type_ids: 0   0   0   0  0     0 0
        #
        # Where "type_ids" are used to indicate whether this is the first
        # sequence or the second sequence. The embedding vectors for `type=0` and
        # `type=1` were learned during pre-training and are added to the wordpiece
        # embedding vector (and position vector). This is not *strictly* necessary
        # since the [SEP] token unambigiously separates the sequences, but it makes
        # it easier for the model to learn the concept of sequences.
        #
        # For classification tasks, the first vector (corresponding to [CLS]) is
        # used as as the "sentence vector". Note that this only makes sense because
        # the entire model is fine-tuned.
        tokens = []
        segment_ids = []

        tokens.append("[CLS]")
        segment_ids.append(0)
        # for i in range(36):
        #     # tokens.append(0)
        #     segment_ids.append(0)

        # tokens.append("[SEP]")
        # segment_ids.append(0)
        for token in caption:
            tokens.append(token)
            segment_ids.append(0)
        tokens.append("[SEP]")
        segment_ids.append(0)

        input_ids = tokenizer.convert_tokens_to_ids(tokens)

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        # input_ids = input_ids[:1] input_ids[1:]
        input_mask = [1] * (len(input_ids))
        image_mask = [1] * (num_boxes)

        # Zero-pad up to the visual sequence length.
        while len(image_mask) < max_region_length:
            image_mask.append(0)
            image_label.append(-1)

        # Zero-pad up to the sequence length.
        while len(input_ids) < max_seq_length:
            input_ids.append(0)
            input_mask.append(0)
            segment_ids.append(0)
            lm_label_ids.append(-1)

        multimodal_mask = image_mask + input_mask
        max_length = max_region_length + max_seq_length

        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(multimodal_mask) == max_length
        assert len(segment_ids) == max_seq_length
        assert len(lm_label_ids) == max_seq_length
        assert len(image_mask) == max_region_length
        assert len(image_label) == max_region_length

        # if example.guid < 5:
        #     logger.info("*** Example ***")
        #     logger.info("guid: %s" % (example.guid))
        #     logger.info("tokens: %s" % " ".join(
        #             [str(x) for x in tokens]))
        #     logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
        #     logger.info("input_mask: %s" % " ".join([str(x) for x in input_mask]))
        #     logger.info(
        #             "segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
        #     logger.info("LM label: %s " % (lm_label_ids))
        #     logger.info("Is next sentence label: %s " % (example.is_next))

        features = InputFeatures(
            input_ids=np.array(input_ids),
            input_mask=np.array(input_mask),
            segment_ids=np.array(segment_ids),
            lm_label_ids=np.array(lm_label_ids),
            is_next=np.array(example.is_next),
            image_feat=image_feat,
            image_target=image_target,
            image_loc=image_loc,
            image_label=np.array(image_label),
            image_mask = np.array(image_mask),
            multimodal_mask = np.array(multimodal_mask)
        )
        return features

    def _truncate_seq_pair(self, tokens_b, max_length):
        """Truncates a sequence pair in place to the maximum length."""

        # This is a simple heuristic which will always truncate the longer sequence
        # one token at a time. This makes more sense than truncating an equal percent
        # of tokens from each, since if one sequence is very short then each token
        # that's truncated likely contains more information than a longer sequence.
        while True:
            total_length = len(tokens_b)
            if total_length <= max_length:
                break

            tokens_b.pop()

    def random_word(self, tokens, tokenizer, span_mask=False, cond_mask=False):
        """
        Masking some random tokens for Language Model task with probabilities as in the original BERT paper.
        :param tokens: list of str, tokenized sentence.
        :param tokenizer: Tokenizer, object used for tokenization (we need it's vocab here)
        :return: (list of str, list of int), masked tokens and related labels for LM prediction
        """
        output_label = []
        
        if cond_mask:
            for i, token in enumerate(tokens):
                output_label.append(-1)
        else:
            if span_mask:
                num_spans = 1
                span_range = int(len(tokens)*0.3)
                span_positions = []
                if span_range > 0:
                    for i in range(num_spans):
                        span_range = int(len(tokens)/num_spans)
                        start = random.randint(span_range*i, span_range*(i+1))
                        for j in range(random.randint(1,span_range)):
                            if start + j <= len(tokens) - 1:
                                span_positions.append(start + j)
                            else:
                                break
                        
                for i, token in enumerate(tokens):
                    if i in span_positions:
                        prob = random.random()
                        if prob < 0.8:
                        # 80% randomly change token to mask token
                            tokens[i] ="[MASK]"
                        elif prob < 0.9:
                            tokens[i] = random.choice(list(tokenizer.vocab.items()))[0]
                        # -> rest 10% randomly keep current token
                        # append current token to output (we will predict these later)
                        try:
                            output_label.append(tokenizer.vocab[token])
                        except KeyError:
                            # For unknown words (should not occur with BPE vocab)
                            output_label.append(tokenizer.vocab["[UNK]"])
                            logger.warning(
                                "Cannot find token '{}' in vocab. Using [UNK] instead".format(token)
                            )
                    else:
                        # no masking token (will be ignored by loss function later)
                        output_label.append(-1)
            else:
                for i, token in enumerate(tokens):
                    prob = random.random()
                    # mask token with 15% probability
                    
                    if prob < 0.15 and not self.visualization:
                        prob /= 0.15

                        # 80% randomly change token to mask token
                        if prob < 0.8:
                            tokens[i] = "[MASK]"

                        # 10% randomly change token to random token
                        elif prob < 0.9:
                            tokens[i] = random.choice(list(tokenizer.vocab.items()))[0]

                        # -> rest 10% randomly keep current token

                        # append current token to output (we will predict these later)
                        try:
                            output_label.append(tokenizer.vocab[token])
                        except KeyError:
                            # For unknown words (should not occur with BPE vocab)
                            output_label.append(tokenizer.vocab["[UNK]"])
                            logger.warning(
                                "Cannot find token '{}' in vocab. Using [UNK] instead".format(token)
                            )
                    else:
                        # no masking token (will be ignored by loss function later)
                        output_label.append(-1)

        return tokens, output_label

    def random_region(self, image_feat, image_loc, num_boxes, span_mask=False, cond_mask=False, image_w=None, image_h=None):
        """
        """
        output_label = []

        if cond_mask:
            for i in range(num_boxes):
                output_label.append(-1)
        else:
            if span_mask:
                # image_center_horizontal, image_center_vertical = image_loc[:,2] - image_loc[:,0], image_loc[:,3] - image_loc[:,1]
                # image_center = np.concatenate((image_center_horizontal[:, np.newaxis], image_center_vertical[:, np.newaxis]), axis=1)
                # image_dist = distance.cdist(image_center, image_center, 'euclidean')
                # _, image_topk = torch.topk(torch.tensor(image_dist), 3, largest=False)
                # image_topk = image_topk.numpy()
                iou = iou_numpy(image_loc, image_loc, image_w, image_h)
                for i in range(num_boxes):
                    prob = random.random()
                    # if i in span_positions:
                    if prob < 0.1 and not self.visualization:
                        image_feat[iou[i] >= 0.4] = 0
                for i in range(num_boxes):
                    if image_feat[i].any() == 0:
                        output_label.append(1)
                    else:
                        output_label.append(-1)
            else:
                for i in range(num_boxes):
                    prob = random.random()
                    # mask token with 15% probability
                    if prob < 0.15 and not self.visualization:
                        prob /= 0.15
                        # 80% randomly change token to mask token
                        if prob < 0.9:
                            image_feat[i] = 0
                        # -> rest 10% randomly keep current token
                        # append current token to output (we will predict these later)
                        output_label.append(1)
                    else:
                        # no masking token (will be ignored by loss function later)
                        output_label.append(-1)

        return image_feat, image_loc, output_label