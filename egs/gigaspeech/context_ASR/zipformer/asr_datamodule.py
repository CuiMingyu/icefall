# Copyright      2023  Xiaomi Corporation     (Author: Yifan Yang)
#
# See ../../../../LICENSE for clarification regarding multiple authors
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


import argparse
import inspect
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

import torch
from lhotse import CutSet, load_manifest, load_manifest_lazy
from lhotse.dataset import (  # noqa F401 for PrecomputedFeatures
    DiscretizedInputAugment,
    DiscretizedInputSpeechRecognitionDataset,
    DynamicBucketingSampler,
    SimpleCutSampler,
)
from lhotse.utils import fix_random_seed
from torch.utils.data import DataLoader

from icefall.utils import str2bool


class _SeedWorkers:
    def __init__(self, seed: int):
        self.seed = seed

    def __call__(self, worker_id: int):
        fix_random_seed(self.seed + worker_id)


class GigaSpeechAsrDataModule:
    """
    DataModule for k2 ASR experiments.
    It assumes there is always one train and valid dataloader,
    but there can be multiple test dataloaders (e.g. GigaSpeech test-clean
    and test-other).

    It contains all the common data pipeline modules used in ASR
    experiments, e.g.:
    - dynamic batch size,
    - bucketing samplers,
    - augmentation,

    This class should be derived for specific corpora used in ASR tasks.
    """

    def __init__(self, args: argparse.Namespace):
        self.args = args

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser):
        group = parser.add_argument_group(
            title="ASR data related options",
            description="These options are used for the preparation of "
            "PyTorch DataLoaders from Lhotse CutSet's -- they control the "
            "effective batch sizes, sampling strategies, applied data "
            "augmentations, etc.",
        )
        group.add_argument(
            "--manifest-dir",
            type=Path,
            default=Path("data/fbank"),
            help="Path to directory with train/valid/test cuts.",
        )
        group.add_argument(
            "--max-duration",
            type=int,
            default=1000,
            help="Maximum pooled recordings duration (seconds) in a "
            "single batch. You can reduce it if it causes CUDA OOM.",
        )
        group.add_argument(
            "--bucketing-sampler",
            type=str2bool,
            default=True,
            help="When enabled, the batches will come from buckets of "
            "similar duration (saves padding frames).",
        )
        group.add_argument(
            "--num-buckets",
            type=int,
            default=30,
            help="The number of buckets for the DynamicBucketingSampler"
            "(you might want to increase it for larger datasets).",
        )
        group.add_argument(
            "--shuffle",
            type=str2bool,
            default=True,
            help="When enabled (=default), the examples will be "
            "shuffled for each epoch.",
        )
        group.add_argument(
            "--drop-last",
            type=str2bool,
            default=True,
            help="Whether to drop last batch. Used by sampler.",
        )
        group.add_argument(
            "--return-cuts",
            type=str2bool,
            default=True,
            help="When enabled, each batch will have the "
            "field: batch['supervisions']['cut'] with the cuts that "
            "were used to construct it.",
        )
        group.add_argument(
            "--num-workers",
            type=int,
            default=2,
            help="The number of training dataloader workers that "
            "collect the batches.",
        )
        group.add_argument(
            "--enable-spec-aug",
            type=str2bool,
            default=True,
            help="When enabled, use SpecAugment for training dataset.",
        )

        group.add_argument(
            "--spec-aug-time-warp-factor",
            type=int,
            default=80,
            help="Used only when --enable-spec-aug is True. "
            "It specifies the factor for time warping in SpecAugment. "
            "Larger values mean more warping. "
            "A value less than 1 means to disable time warp.",
        )

        group.add_argument(
            "--enable-gaussian-noise",
            type=str2bool,
            default=True,
        )

        group.add_argument(
            "--input-strategy",
            type=str,
            default="AudioSamples",
            help="AudioSamples or PrecomputedFeatures",
        )

        # GigaSpeech specific arguments
        group.add_argument(
            "--subset",
            type=str,
            default="M",
            help="Select the GigaSpeech subset (XS|S|M|L|XL)",
        )
        group.add_argument(
            "--small-dev",
            type=str2bool,
            default=False,
            help="Should we use only 1000 utterances for dev (speeds up training)",
        )

    def train_dataloaders(
        self,
        cuts_train: CutSet,
        sampler_state_dict: Optional[Dict[str, Any]] = None,
    ) -> DataLoader:
        """
        Args:
          cuts_train:
            CutSet for training.
          sampler_state_dict:
            The state dict for the training sampler.
        """
        input_transforms = []
        if self.args.enable_spec_aug:
            logging.info("Enable DiscretizedInputAugment")
            logging.info(f"Time warp factor: {self.args.spec_aug_time_warp_factor}")
            # Set the value of num_frame_masks according to Lhotse's version.
            # In different Lhotse's versions, the default of num_frame_masks is
            # different.
            num_frame_masks = 10
            num_frame_masks_parameter = inspect.signature(
                DiscretizedInputAugment.__init__
            ).parameters["num_frame_masks"]
            if num_frame_masks_parameter.default == 1:
                num_frame_masks = 2
            logging.info(f"Num frame mask: {num_frame_masks}")
            input_transforms.append(
                DiscretizedInputAugment(
                    token_type="wavlm",
                    time_warp_factor=self.args.spec_aug_time_warp_factor,
                    num_frame_masks=num_frame_masks,
                    tokens_mask_size=27,
                    num_token_masks=4,
                    frames_mask_size=100,
                )
            )
        else:
            logging.info("Disable DiscretizedInputAugment")

        logging.info("About to create train dataset")
        train = DiscretizedInputSpeechRecognitionDataset(
            field="discrete_tokens",
            num_tokens=2000,
            frequency_size=80,
            token_type="wavlm",
            input_transforms=input_transforms,
        )

        if self.args.bucketing_sampler:
            logging.info("Using DynamicBucketingSampler.")
            train_sampler = DynamicBucketingSampler(
                cuts_train,
                max_duration=self.args.max_duration,
                shuffle=self.args.shuffle,
                num_buckets=self.args.num_buckets,
                drop_last=self.args.drop_last,
            )
        else:
            logging.info("Using SimpleCutSampler.")
            train_sampler = SimpleCutSampler(
                cuts_train,
                max_duration=self.args.max_duration,
                shuffle=self.args.shuffle,
            )
        logging.info("About to create train dataloader")

        if sampler_state_dict is not None:
            logging.info("Loading sampler state dict")
            train_sampler.load_state_dict(sampler_state_dict)

        # 'seed' is derived from the current random state, which will have
        # previously been set in the main process.
        #seed = torch.randint(0, 100000, ()).item()
        seed = 42
        worker_init_fn = _SeedWorkers(seed)

        train_dl = DataLoader(
            train,
            sampler=train_sampler,
            batch_size=None,
            num_workers=self.args.num_workers,
            persistent_workers=False,
            worker_init_fn=worker_init_fn,
        )

        return train_dl

    def valid_dataloaders(self, cuts_valid: CutSet) -> DataLoader:
        logging.info("About to create dev dataset")
        validate = DiscretizedInputSpeechRecognitionDataset(
            field="discrete_tokens",
            num_tokens=2000,
            token_type="wavlm",
        )
        valid_sampler = DynamicBucketingSampler(
            cuts_valid,
            max_duration=self.args.max_duration,
            shuffle=False,
        )
        logging.info("About to create dev dataloader")
        valid_dl = DataLoader(
            validate,
            sampler=valid_sampler,
            batch_size=None,
            num_workers=2,
            persistent_workers=False,
        )

        return valid_dl

    def test_dataloaders(self, cuts: CutSet) -> DataLoader:
        logging.debug("About to create test dataset")
        test = DiscretizedInputSpeechRecognitionDataset(
            field="discrete_tokens",
            num_tokens=2000,
            token_type="wavlm",
        )
        sampler = DynamicBucketingSampler(
            cuts,
            max_duration=self.args.max_duration,
            shuffle=False,
        )
        logging.debug("About to create test dataloader")
        test_dl = DataLoader(
            test,
            batch_size=None,
            sampler=sampler,
            num_workers=self.args.num_workers,
        )
        return test_dl

    @lru_cache()
    def train_cuts(self) -> CutSet:
        if self.args.subset == "M":
            logging.info("About to get train cuts")
            train_cuts = load_manifest_lazy(
                #self.args.manifest_dir / "gigaspeech_cuts_M_transform.jsonl.gz"
                #self.args.manifest_dir / "gigaspeech_cuts_M_fbank_disc_p.jsonl.gz"
                self.args.manifest_dir / "gigaspeech_cuts_M_future.jsonl.gz"
                #self.args.manifest_dir / "gigaspeech_cuts_M.jsonl.gz"
            )
            logging.info("About to get train sp0.9 cuts")
            train_cuts_sp_0_9 = load_manifest_lazy(
                #self.args.manifest_dir / "gigaspeech_cuts_M-sp0_9_transform.jsonl.gz"
                #self.args.manifest_dir / "gigaspeech_cuts_M-sp0_9_fbank_disc_p.jsonl.gz"
                self.args.manifest_dir / "gigaspeech_cuts_M-sp0_9_future.jsonl.gz"
                #self.args.manifest_dir / "gigaspeech_cuts_M-sp0_9.jsonl.gz"
            )
            logging.info("About to get train sp1.1 cuts")
            train_cuts_sp_1_1 = load_manifest_lazy(
                #self.args.manifest_dir / "gigaspeech_cuts_M-sp1_1_transform.jsonl.gz"
                #self.args.manifest_dir / "gigaspeech_cuts_M-sp1_1_fbank_disc_p.jsonl.gz"
                self.args.manifest_dir / "gigaspeech_cuts_M-sp1_1_future.jsonl.gz"
                #self.args.manifest_dir / "gigaspeech_cuts_M-sp1_1.jsonl.gz"
            )
            return CutSet.mux(
                train_cuts,
                train_cuts_sp_0_9,
                train_cuts_sp_1_1,
                weights=[
                    909401,  # len(train_cuts)
                    909401,  # len(train_cuts_sp_0_9)
                    909401,  # len(train_cuts_sp_1_1)
                ],
            )
        elif self.args.subset == "XL":
            logging.info("About to get train cuts")
            train_cuts = load_manifest_lazy(
                self.args.manifest_dir / "gigaspeech_cuts_XL.jsonl.gz"
            )
            logging.info("About to get train sp0.9 cuts")
            train_cuts_sp_0_9 = load_manifest_lazy(
                self.args.manifest_dir / "gigaspeech_cuts_XL-sp0_9.jsonl.gz"
            )
            logging.info("About to get train sp1.1 cuts")
            train_cuts_sp_1_1 = load_manifest_lazy(
                self.args.manifest_dir / "gigaspeech_cuts_XL-sp1_1.jsonl.gz"
            )
            return CutSet.mux(
                train_cuts,
                train_cuts_sp_0_9,
                train_cuts_sp_1_1,
                weights=[
                    8277188,  # len(train_cuts)
                    8277188,  # len(train_cuts_sp_0_9)
                    8277188,  # len(train_cuts_sp_1_1)
                ],
            )

    @lru_cache()
    def dev_cuts(self) -> CutSet:
        logging.info("About to get dev cuts")
        cuts_valid = load_manifest_lazy(
            #self.args.manifest_dir / "gigaspeech_cuts_DEV_transform.jsonl.gz"
            #self.args.manifest_dir / "gigaspeech_cuts_DEV_fbank_disc_p.jsonl.gz"
            self.args.manifest_dir / "gigaspeech_cuts_DEV_future.jsonl.gz"
            #self.args.manifest_dir / "gigaspeech_cuts_DEV.jsonl.gz"
        )
        if self.args.small_dev:
            return cuts_valid.subset(first=1000)
        else:
            return cuts_valid

    @lru_cache()
    def test_cuts(self) -> CutSet:
        logging.info("About to get test cuts")
        return load_manifest_lazy(
            #self.args.manifest_dir / "gigaspeech_cuts_TEST_transform.jsonl.gz"
            #self.args.manifest_dir / "gigaspeech_cuts_TEST_fbank_disc_p.jsonl.gz"
            self.args.manifest_dir / "gigaspeech_cuts_TEST_future.jsonl.gz"
            #self.args.manifest_dir / "gigaspeech_cuts_TEST.jsonl.gz"
        )
