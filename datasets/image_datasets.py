# Standard Library
import random
from pathlib import Path
from typing import Optional

# Third-Party Library
import numpy as np

# Torch Library
import torch
import torch.utils.data as data

# My Library
from .frame import get_frame_reader

from utils.io import load_json
from utils.color import error, red
from utils.annotation import Annotation


def get_classes(class_txt: Path) -> dict[str, int]:
    with class_txt.open(mode="r") as f:
        classes: list[str] = f.readlines()
    return {c.strip(): i for i, c in enumerate(classes)}


class ActionSpotDataset(data.Dataset):

    def __init__(
        self,
        # dict of class names to idx
        classes: dict[str, int],
        # Path to the label json
        label_file: Path,
        # Path to the frames
        clip_dir: Path,
        # Modality of the frames, [rgb, bw, flow]
        modality: str,
        # Length of the clip, i.e. frame num
        clip_len: int,
        # Length of the datasets, i.e., clip num
        dataset_len: int,
        # Disable random augmentations to each frame in a clip
        is_eval: bool = True,
        # Dimension to crop the clip
        crop_dim: Optional[int] = None,
        # Stride to sample the clip, >1 to downsample the video
        frame_sample_stride: int = 1,
        # If apply the same random crop augmentation to each frame in a clip
        same_crop_transform: bool = True,
        # Dilate ground truth labels
        dilate_len: int = 0,
        mixup: bool = False,
        # Number of the frames to pad before/after the clip
        n_pad_frames: int = 5,
        # Sample event ratio
        event_sample_rate: float = -1
    ) -> None:
        super().__init__()

        # save the parameters
        self.label_file: Path = label_file
        self.clip_labels: list[Annotation] = load_json(
            label_file)
        self.classes_dict: dict[str, int] = classes
        self.clip_indexes: dict[str, int] = {
            x["video"]: i for i, x in enumerate(self.clip_labels)}
        self.is_eval = is_eval
        self.dilate_len = dilate_len
        self.event_sample_rate = event_sample_rate
        self.mixup = mixup

        # parameters that need verify
        self.clip_len: int = clip_len
        assert clip_len > 0, error(
            f"clip len should be greater than 0, got: {red(clip_len, True)}")

        self.frame_sample_stride: int = frame_sample_stride
        assert frame_sample_stride > 0, error(
            f"frame_sample_stride should be greater than 0, got: {red(frame_sample_stride, True)}")

        self.dataset_len = dataset_len
        assert dataset_len > 0, error(
            f"dataset_len should be greater than 0, got: {red(dataset_len, True)}")

        self.n_pad_frames = n_pad_frames
        assert n_pad_frames >= 0, error(
            f"n_pad_frames should be greater equal than 0, got: {red(n_pad_frames, True)}")

        # Sample based on foreground labels
        if self.event_sample_rate > 0:
            self.flat_labels = []
            for i, x, in enumerate(self.clip_labels):
                for event in x["events"]:
                    if event["frame"] < x["num_frames"]:
                        self.flat_labels.append((i, event["frame"]))

        # Sample based on the clip length
        num_frames = [c["num_frames"] for c in self.clip_labels]
        self.uniform_sample_weight = np.array(num_frames) / np.sum(num_frames)

        # Frame Reader
        self.frame_reader = get_frame_reader(
            clip_dir=clip_dir, is_eval=is_eval, modality=modality, crop_dim=crop_dim,
            same_crop_transform=same_crop_transform, multi_crop=False)

    def sample_clip(self) -> tuple[Annotation, int]:
        """
        Uniformly samples a clip label and start frame based on specified parameters.

        Returns:
            tuple[Annotation, int]: A tuple containing the sampled clip label and start frame.
        """
        clip_label = random.choices(
            self.clip_labels, weights=self.uniform_sample_weight)[0]

        clip_frames = clip_label["num_frames"]
        # every time we sample a same clip, we would like it having some frame-shifting, i.e.
        # the first time we sample clip A from frame 0 to frame 100
        # the next time we sample clip A again, we would like it from 10-110
        # so with some frame-shifting, we increase the total amount of training examples
        start_frame = -self.n_pad_frames * self.frame_sample_stride + random.randint(
            0, max(0, clip_frames - 1 + (2 * self.n_pad_frames - self.clip_len) * self.frame_sample_stride))
        return clip_label, start_frame

    def sample_event(self) -> tuple[Annotation, int]:
        """
        Uniformly samples a event label and start frame based on specified parameters.

        Returns:
            tuple[Annotation, int]: A tuple containing the sampled event label and start frame.
        """
        video_idx, frame_idx = random.choices(self.flat_labels)[0]
        clip_label = self.clip_labels[video_idx]
        video_len = clip_label['num_frames']

        lower_bound = max(
            -self.n_pad_frames * self.frame_sample_stride,
            frame_idx - self.clip_len * self.frame_sample_stride + 1)
        upper_bound = min(
            video_len - 1 + (self.n_pad_frames - self.clip_len) *
            self.frame_sample_stride,
            frame_idx)

        start_frame = random.randint(lower_bound, upper_bound) \
            if upper_bound > lower_bound else lower_bound

        assert start_frame <= frame_idx
        assert start_frame + self.clip_len > frame_idx
        return clip_label, start_frame

    def get_sample(self) -> tuple[Annotation, int]:
        # because event is rarely sparse in the clip, so we need to sample
        # the event to increase the training examples
        if self.event_sample_rate > 0 and random.random() > self.event_sample_rate:
            clip_label, start_frame = self.sample_event()
        else:
            clip_label, start_frame = self.sample_clip()
        return clip_label, start_frame

    def get_example(self) -> dict[str, torch.FloatTensor | np.ndarray | int]:
        clip_label, start_frame = self.get_sample()

        # make labels
        labels = np.zeros(self.clip_len, np.int64)
        for event in clip_label["events"]:
            event_frame = event["frame"]

            # calculate the index of the frame
            label_index = (
                event_frame - start_frame) // self.frame_sample_stride
            if (label_index >= -self.dilate_len and label_index < self.clip_len + self.dilate_len):
                label = self.classes_dict[event['label']]
                for i in range(max(0, label_index - self.dilate_len), min(self.clip_len, label_index + self.dilate_len + 1)):
                    labels[i] = label

        # load frames
        frames = self.frame_reader.load_frames(
            clip_name=clip_label["video"], start_frame=start_frame,
            end_frame=start_frame + self.clip_len * self.frame_sample_stride,
            pad_end_frame=True, frame_sample_stride=self.frame_sample_stride, random_sample=not self.is_eval
        )

        return {"frame": frames, "label": labels, "contains_event": int(labels.sum() > 0)}

    def __getitem__(self, unused):
        example = self.get_example()
        while example["frame"] is None:
            example = self.get_example()
        return example

    def __len__(self):
        return self.dataset_len


if __name__ == "__main__":
    import torch.utils.data as data

    from utils.config import parse_config_yaml

    config = parse_config_yaml(
        Path(__file__).parent.joinpath("../config/vlh.yaml"))

    base_dir = Path(config["variables"]["basedir"])

    for dataset_name, dataset_config in config["datasets"].items():

        clip_dir = Path(dataset_config["clip_dir"])
        class_file = Path(dataset_config["class_file"])

        classes = get_classes(class_file)

        train_dataset = ActionSpotDataset(
            classes=classes,
            label_file=base_dir.joinpath(f"tools/{dataset_name}/train.json"),
            clip_dir=clip_dir,
            modality="rgb",
            clip_len=100,
            dataset_len=50000,
            is_eval=False
        )

        val_dataset = ActionSpotDataset(
            classes=classes,
            label_file=base_dir.joinpath(f"tools/{dataset_name}/val.json"),
            clip_dir=clip_dir,
            modality="rgb",
            clip_len=120,
            dataset_len=50000 // 4,
            is_eval=True,
            same_crop_transform=True,
        )

        test_dataset = ActionSpotDataset(
            classes=classes,
            label_file=base_dir.joinpath(f"tools/{dataset_name}/test.json"),
            clip_dir=clip_dir,
            modality="rgb",
            clip_len=130,
            dataset_len=50000 // 4,
            is_eval=True,
            same_crop_transform=True,
        )

        train_loader = data.DataLoader(train_dataset)
        val_loader = data.DataLoader(val_dataset)
        test_loader = data.DataLoader(test_dataset)

        print(f"test {dataset_name}")

        print("test train datasets")
        for i in train_loader:
            print(i["contains_event"])
            print(i["frame"].shape, i["frame"].size(1) == 100)
            print(i["label"].shape, i["label"].size(1) == 100)
            break

        print("test val datasets")
        for i in val_loader:
            print(i["contains_event"])
            print(i["frame"].shape, i["frame"].size(1) == 120)
            print(i["label"].shape, i["label"].size(1) == 120)
            break

        print("test test datasets")
        for i in test_loader:
            print(i["contains_event"])
            print(i["frame"].shape, i["frame"].size(1) == 130)
            print(i["label"].shape, i["label"].size(1) == 130)
            break