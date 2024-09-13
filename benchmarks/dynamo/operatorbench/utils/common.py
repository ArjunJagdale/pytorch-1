
from typing import Optional, List, Dict
import pathlib
import dataclasses
import torch
from enum import Enum


@dataclasses.dataclass
class OperatorConfig:
    name: str
    variant: str
    device: str
    extra_args: List[str]
    extra_env: Optional[Dict[str, str]] = None
    output_dir: Optional[pathlib.Path] = None


class BenchmarkResults:
    def __init__(self, durations: List[float]):
        self.durations = durations

    def median(self) -> float:
        # return np.median(self.durations)
        pass


class Device(Enum):
    CPU = "cpu"
    CUDA = "cuda"


@dataclasses.dataclass
class BenchmarkConfig:
    device: Device
    dtype: torch.dtype
    phase: str
    max_samples: int
    repeat: int
    single_run: bool


class Phase(Enum):
    FORWARD = "forward"
    BACKWARD = "backward"
    FULL = "full"


dtype_mapping = {
    "bfloat16": torch.bfloat16,
    "float16": torch.float16,
    "float32": torch.float32,
}
