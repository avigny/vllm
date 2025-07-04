# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Optional, TypeVar, Union

import torch
import torch.nn as nn

from .interfaces_base import VllmModelForPooling, is_pooling_model

if TYPE_CHECKING:
    from vllm.model_executor.layers.pooler import PoolingType

_T = TypeVar("_T", bound=type[nn.Module])

_GENERATE_SUFFIXES = [
    "ForCausalLM",
    "ForConditionalGeneration",
    "ChatModel",
    "LMHeadModel",
]


def _get_pooling_model_name(orig_model_name: str, pooling_suffix: str) -> str:
    model_name = orig_model_name

    for generate_suffix in _GENERATE_SUFFIXES:
        model_name = model_name.removesuffix(generate_suffix)

    return model_name + pooling_suffix


def _create_pooling_model_cls(
    orig_cls: _T,
    *,
    default_pooling_type: "PoolingType",
    default_normalize: bool,
    default_softmax: bool,
) -> _T:
    # Lazy import
    from vllm.config import VllmConfig
    from vllm.model_executor.layers.pooler import Pooler, PoolerOutput
    from vllm.model_executor.pooling_metadata import PoolingMetadata

    from .utils import AutoWeightsLoader, WeightsMapper

    class ModelForPooling(orig_cls, VllmModelForPooling):

        def __init__(
            self,
            *,
            vllm_config: "VllmConfig",
            prefix: str = "",
            **kwargs: Any,
        ) -> None:
            super().__init__(vllm_config=vllm_config, prefix=prefix, **kwargs)

            # These are not used in pooling models
            for attr in ("lm_head", "logits_processor"):
                if hasattr(self, attr):
                    delattr(self, attr)

            pooler_config = vllm_config.model_config.pooler_config
            assert pooler_config is not None

            # If the model already defines a pooler instance, don't overwrite it
            if not getattr(self, "_pooler", None):
                self._pooler = Pooler.from_config_with_defaults(
                    pooler_config,
                    pooling_type=default_pooling_type,
                    normalize=default_normalize,
                    softmax=default_softmax,
                )

        def pooler(
            self,
            hidden_states: torch.Tensor,
            pooling_metadata: PoolingMetadata,
        ) -> PoolerOutput:
            return self._pooler(hidden_states, pooling_metadata)

        def load_weights(self, weights: Iterable[tuple[str, torch.Tensor]]):
            # TODO: Support uninitialized params tracking

            # We have deleted this attribute, so don't load it
            weights = ((name, data) for name, data in weights
                       if not name.startswith("lm_head."))

            # If `*ForCausalLM` defines `load_weights` on the inner model
            # and there are no other inner modules with parameters,
            # we support loading from both `*Model` and `*ForCausalLM`
            if hasattr(self, "model") and hasattr(self.model, "load_weights"):
                # Whether only `self.model` contains parameters
                model_is_only_param = all(
                    name == "model" or next(child.parameters(), None) is None
                    for name, child in self.named_children())

                if model_is_only_param:
                    mapper = WeightsMapper(orig_to_new_prefix={"model.": ""})
                    weights = mapper.apply(weights)

                    loaded_params = self.model.load_weights(weights)
                    loaded_params = {f"model.{name}" for name in loaded_params}
                    return loaded_params

            # For most other models
            if hasattr(orig_cls, "load_weights"):
                return orig_cls.load_weights(self, weights)  # type: ignore
            # Fallback
            else:
                loader = AutoWeightsLoader(self)
                return loader.load_weights(weights)

    return ModelForPooling  # type: ignore


def as_embedding_model(cls: _T) -> _T:
    """
    Subclass an existing vLLM model to support embeddings.

    By default, the embeddings of the whole prompt are extracted from the
    normalized hidden state corresponding to the last token.

    Note:
        We assume that no extra layers are added to the original model;
        please implement your own model if this is not the case.
    """
    # Avoid modifying existing embedding models
    if is_pooling_model(cls):
        return cls

    # Lazy import
    from vllm.model_executor.layers.pooler import PoolingType

    ModelForEmbedding = _create_pooling_model_cls(
        cls,
        default_pooling_type=PoolingType.LAST,
        default_normalize=True,
        default_softmax=False,
    )
    ModelForEmbedding.__name__ = \
        _get_pooling_model_name(cls.__name__, "ForEmbedding")

    return ModelForEmbedding  # type: ignore


def as_seq_cls_model(cls: _T) -> _T:
    """
    Subclass an existing vLLM model to support classify and score tasks.

    By default, the class probabilities are extracted from the softmaxed
    hidden state corresponding to the last token.

    Note:
        We assume that the classification head is a single linear layer
        stored as the attribute `score` of the top-level model;
        please implement your own model if this is not the case.
    """
    # Avoid modifying existing classification models
    if is_pooling_model(cls):
        return cls

    # Lazy import
    from vllm.config import VllmConfig
    from vllm.model_executor.layers.linear import RowParallelLinear
    from vllm.model_executor.layers.pooler import PoolerOutput, PoolingType
    from vllm.model_executor.models.interfaces import SupportsCrossEncoding
    from vllm.model_executor.pooling_metadata import PoolingMetadata
    from vllm.sequence import IntermediateTensors

    from .utils import maybe_prefix

    ModelForPooling = _create_pooling_model_cls(
        cls,
        default_pooling_type=PoolingType.LAST,
        default_normalize=False,
        default_softmax=True,
    )

    class ModelForSequenceClassification(ModelForPooling,
                                         SupportsCrossEncoding):

        def __init__(
            self,
            *,
            vllm_config: "VllmConfig",
            prefix: str = "",
            **kwargs: Any,
        ) -> None:
            super().__init__(vllm_config=vllm_config, prefix=prefix, **kwargs)

            config = vllm_config.model_config.hf_config
            quant_config = vllm_config.quant_config

            self.task = vllm_config.model_config.task
            self.pooling_type = (
                vllm_config.model_config.pooler_config.pooling_type)

            self.score = RowParallelLinear(config.hidden_size,
                                           config.num_labels,
                                           quant_config=quant_config,
                                           input_is_parallel=False,
                                           bias=False,
                                           prefix=maybe_prefix(
                                               prefix, "score"))

        def forward(
            self,
            input_ids: torch.Tensor,
            positions: torch.Tensor,
            intermediate_tensors: Optional[IntermediateTensors] = None,
            inputs_embeds: Optional[torch.Tensor] = None,
        ) -> torch.Tensor:
            return super().forward(input_ids, positions, intermediate_tensors,
                                   inputs_embeds)

        def pooler(
            self,
            hidden_states: Union[torch.Tensor, list[torch.Tensor]],
            pooling_metadata: PoolingMetadata,
        ) -> PoolerOutput:

            def get_logits(hidden_states):
                if isinstance(hidden_states, list):
                    logits = [self.score(state)[0] for state in hidden_states]
                else:
                    logits, _ = self.score(hidden_states)
                return logits

            if self.pooling_type == PoolingType.ALL:
                logits = get_logits(hidden_states)
                return self._pooler(logits, pooling_metadata)
            else:
                hidden_states = self._pooler.extract_states(
                    hidden_states, pooling_metadata)
                logits = get_logits(hidden_states)
                pooled_data = self._pooler.head(logits, pooling_metadata)

                pooled_outputs = [
                    self._pooler.build_output(data) for data in pooled_data
                ]
                return PoolerOutput(outputs=pooled_outputs)


    ModelForSequenceClassification.__name__ = \
        _get_pooling_model_name(cls.__name__, "ForSequenceClassification")

    return ModelForSequenceClassification  # type: ignore


def as_reward_model(cls: _T) -> _T:
    """
    Subclass an existing vLLM model to support reward modeling.

    By default, we return the hidden states of each token directly.

    Note:
        We assume that no extra layers are added to the original model;
        please implement your own model if this is not the case.
    """
    # Avoid modifying existing reward models
    if is_pooling_model(cls):
        return cls

    # Lazy import
    from vllm.model_executor.layers.pooler import PoolingType

    ModelForReward = _create_pooling_model_cls(
        cls,
        default_pooling_type=PoolingType.ALL,
        default_normalize=False,
        default_softmax=False,
    )

    ModelForReward.__name__ = \
        _get_pooling_model_name(cls.__name__, "ForReward")

    return ModelForReward  # type: ignore
