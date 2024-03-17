"""
Laboratory work.

Working with Large Language Models.
"""
# pylint: disable=too-few-public-methods, undefined-variable, too-many-arguments, super-init-not-called, duplicate-code
from collections import namedtuple
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd
from datasets import load_dataset
from torchinfo import summary
from transformers import AutoTokenizer, GPTNeoXForCausalLM

try:
    import torch
    from torch.utils.data.dataset import Dataset
except ImportError:
    print('Library "torch" not installed. Failed to import.')
    Dataset = dict
    torch = namedtuple('torch', 'no_grad')(lambda: lambda fn: fn)  # type: ignore

try:
    from pandas import DataFrame
except ImportError:
    print('Library "pandas" not installed. Failed to import.')
    DataFrame = dict  # type: ignore

from core_utils.llm.llm_pipeline import AbstractLLMPipeline
from core_utils.llm.metrics import Metrics
from core_utils.llm.raw_data_importer import AbstractRawDataImporter
from core_utils.llm.raw_data_preprocessor import AbstractRawDataPreprocessor, ColumnNames
from core_utils.llm.task_evaluator import AbstractTaskEvaluator
from core_utils.llm.time_decorator import report_time


class RawDataImporter(AbstractRawDataImporter):
    """
    A class that imports the HuggingFace dataset.
    """

    @report_time
    def obtain(self):
        """
        Download a dataset.

        Raises:
            TypeError: In case of downloaded dataset is not pd.DataFrame
        """
        self._raw_data = load_dataset(self._hf_name,
                                      split='train').to_pandas()

        if not isinstance(self._raw_data, pd.DataFrame):
            raise TypeError

    @property
    def raw_data(self) -> DataFrame:
        """
        Property for original dataset in a table format.

        Returns:
            pandas.DataFrame: A dataset in a table format
        """
        if not isinstance(self._raw_data, pd.DataFrame):
            raise TypeError

        return self._raw_data


class RawDataPreprocessor(AbstractRawDataPreprocessor):
    """
    A class that analyzes and preprocesses a dataset.
    """

    def analyze(self) -> dict:
        """
        Analyze a dataset.

        Returns:
            dict: Dataset key properties
        """

        empty_rows_free = self._raw_data.replace("", pd.NA).dropna().reset_index()

        return {'dataset_number_of_samples': self._raw_data.shape[0],
                'dataset_columns': self._raw_data.shape[1],
                'dataset_duplicates': self._raw_data.duplicated().sum(),
                'dataset_empty_rows': self._raw_data.replace("", pd.NA).isna().sum().sum(),
                'dataset_sample_min_len': len(min(empty_rows_free["instruction"], key=len)),
                'dataset_sample_max_len': len(max(empty_rows_free["instruction"], key=len)),
                }

    @report_time
    def transform(self):
        """
        Apply preprocessing transformations to the raw dataset.
        """
        self._data = (self._raw_data
                      .rename(columns={'instruction': 'question', 'output': 'target'})
                      .drop(columns=['input', 'text']))


class TaskDataset(Dataset):
    """
    A class that converts pd.DataFrame to Dataset and works with it.
    """

    def __init__(self, data: DataFrame):
        """
        Initialize an instance of TaskDataset.

        Args:
            data (pandas.DataFrame): Original data
        """
        self._data = data

    def __len__(self) -> int:
        """
        Return the number of items in the dataset.

        Returns:
            int: The number of items in the dataset
        """
        return len(self._data)

    def __getitem__(self, index: int) -> tuple[str, ...]:
        """
        Retrieve an item from the dataset by index.

        Args:
            index (int): Index of sample in dataset

        Returns:
            tuple[str, ...]: The item to be received
        """
        return self._data.iloc[index]['question'], self._data.iloc[index]['target']

    @property
    def data(self) -> DataFrame:
        """
        Property with access to preprocessed DataFrame.

        Returns:
            pandas.DataFrame: Preprocessed DataFrame
        """
        return self._data


class LLMPipeline(AbstractLLMPipeline):
    """
    A class that initializes a model, analyzes its properties and infers it.
    """
    _model: torch.nn.Module

    def __init__(
            self,
            model_name: str,
            dataset: TaskDataset,
            max_length: int,
            batch_size: int,
            device: str
    ):
        """
        Initialize an instance of LLMPipeline.

        Args:
            model_name (str): The name of the pre-trained model
            dataset (TaskDataset): The dataset used
            max_length (int): The maximum length of generated sequence
            batch_size (int): The size of the batch inside DataLoader
            device (str): The device for inference
        """
        super().__init__(model_name, dataset, max_length, batch_size, device)

        self._tokenizer = AutoTokenizer.from_pretrained(model_name, padding_side='left')
        self._tokenizer.pad_token = self._tokenizer.eos_token
        self._model = GPTNeoXForCausalLM.from_pretrained(self._model_name)

    def analyze_model(self) -> dict:
        """
        Analyze model computing properties.

        Returns:
            dict: Properties of a model
        """
        tensor_data = torch.ones(1, self._model.config.max_position_embeddings, dtype=torch.long)
        input_data = {'attention_mask': tensor_data,
                      "input_ids": tensor_data}
        analytics = summary(self._model, input_data=input_data, verbose=False)
        return {
            "embedding_size": self._model.config.max_position_embeddings,
            "input_shape": {'attention_mask': list(analytics.input_size['attention_mask']),
                            'input_ids': list(analytics.input_size['input_ids'])},
            "max_context_length": self._model.config.max_length,
            "num_trainable_params": analytics.trainable_params,
            "output_shape": analytics.summary_list[-1].output_size,
            "size": analytics.total_param_bytes,
            "vocab_size": self._model.config.vocab_size
        }

    @report_time
    def infer_sample(self, sample: tuple[str, ...]) -> str | None:
        """
        Infer model on a single sample.

        Args:
            sample (tuple[str, ...]): The given sample for inference with model

        Returns:
            str | None: A prediction
        """
        tokens = self._tokenizer(sample[0],
                                 max_length=self._max_length,
                                 padding=True,
                                 truncation=True,
                                 return_tensors='pt')
        output_tokens = self._model.generate(**tokens, max_length=self._max_length)
        return self._tokenizer.batch_decode(output_tokens, skip_special_tokens=True)[0][len(sample[0]) + 1:]

    @report_time
    def infer_dataset(self) -> DataFrame:
        """
        Infer model on a whole dataset.

        Returns:
            pd.DataFrame: Data with predictions
        """

    @torch.no_grad()
    def _infer_batch(self, sample_batch: Sequence[tuple[str, ...]]) -> list[str]:
        """
        Infer model on a single batch.

        Args:
            sample_batch (Sequence[tuple[str, ...]]): Batch to infer the model

        Returns:
            list[str]: Model predictions as strings
        """


class TaskEvaluator(AbstractTaskEvaluator):
    """
    A class that compares prediction quality using the specified metric.
    """

    def __init__(self, data_path: Path, metrics: Iterable[Metrics]) -> None:
        """
        Initialize an instance of Evaluator.

        Args:
            data_path (pathlib.Path): Path to predictions
            metrics (Iterable[Metrics]): List of metrics to check
        """

    @report_time
    def run(self) -> dict | None:
        """
        Evaluate the predictions against the references using the specified metric.

        Returns:
            dict | None: A dictionary containing information about the calculated metric
        """
