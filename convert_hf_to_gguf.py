#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import ast
import logging
import argparse
import contextlib
import json
import os
import re
import sys
from enum import IntEnum
from pathlib import Path
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Callable, ContextManager, Iterable, Iterator, Literal, Sequence, TypeVar, cast
from itertools import chain
from transformers import AutoConfig

import math
import numpy as np
import torch

if TYPE_CHECKING:
    from torch import Tensor

if 'NO_LOCAL_GGUF' not in os.environ:
    sys.path.insert(1, str(Path(__file__).parent / 'gguf-py'))
import gguf

logger = logging.getLogger("hf-to-gguf")


###### MODEL DEFINITIONS ######

class SentencePieceTokenTypes(IntEnum):
    NORMAL = 1
    UNKNOWN = 2
    CONTROL = 3
    USER_DEFINED = 4
    UNUSED = 5
    BYTE = 6


class ModelType(IntEnum):
    TEXT = 1
    MMPROJ = 2


AnyModel = TypeVar("AnyModel", bound="type[ModelBase]")


class ModelBase:
    _model_classes: dict[ModelType, dict[str, type[ModelBase]]] = {
        ModelType.TEXT: {},
        ModelType.MMPROJ: {},
    }

    dir_model: Path
    ftype: gguf.LlamaFileType
    fname_out: Path
    is_big_endian: bool
    endianess: gguf.GGUFEndian
    use_temp_file: bool
    lazy: bool
    part_names: list[str]
    is_safetensors: bool
    hparams: dict[str, Any]
    tensor_names: set[str] | None
    gguf_writer: gguf.GGUFWriter
    model_name: str | None
    metadata_override: Path | None
    dir_model_card: Path
    remote_hf_model_id: str | None

    # subclasses should define this!
    model_arch: gguf.MODEL_ARCH

    # subclasses should initialize this!
    block_count: int
    tensor_map: gguf.TensorNameMap

    def __init__(self, dir_model: Path, ftype: gguf.LlamaFileType, fname_out: Path, *, is_big_endian: bool = False,
                 use_temp_file: bool = False, eager: bool = False,
                 metadata_override: Path | None = None, model_name: str | None = None,
                 split_max_tensors: int = 0, split_max_size: int = 0, dry_run: bool = False,
                 small_first_shard: bool = False, hparams: dict[str, Any] | None = None, remote_hf_model_id: str | None = None):
        if type(self) is ModelBase or \
                type(self) is TextModel or \
                type(self) is MmprojModel:
            raise TypeError(f"{type(self).__name__!r} should not be directly instantiated")

        self.dir_model = dir_model
        self.ftype = ftype
        self.fname_out = fname_out
        self.is_big_endian = is_big_endian
        self.endianess = gguf.GGUFEndian.BIG if is_big_endian else gguf.GGUFEndian.LITTLE
        self.use_temp_file = use_temp_file
        self.lazy = not eager or (remote_hf_model_id is not None)
        self.remote_hf_model_id = remote_hf_model_id
        if remote_hf_model_id is not None:
            self.is_safetensors = True

            def get_remote_tensors() -> Iterator[tuple[str, Tensor]]:
                logger.info(f"Using remote model with HuggingFace id: {remote_hf_model_id}")
                remote_tensors = gguf.utility.SafetensorRemote.get_list_tensors_hf_model(remote_hf_model_id)
                self.tensor_names = set(name for name in remote_tensors.keys())
                for name, remote_tensor in gguf.utility.SafetensorRemote.get_list_tensors_hf_model(remote_hf_model_id).items():
                    yield (name, LazyTorchTensor.from_remote_tensor(remote_tensor))

            self.get_tensors = get_remote_tensors
        else:
            self.part_names = ModelBase.get_model_part_names(self.dir_model, "model", ".safetensors")
            self.is_safetensors = len(self.part_names) > 0
            if not self.is_safetensors:
                self.part_names = ModelBase.get_model_part_names(self.dir_model, "pytorch_model", ".bin")
        self.hparams = ModelBase.load_hparams(self.dir_model) if hparams is None else hparams
        self.tensor_names = None
        self.metadata_override = metadata_override
        self.model_name = model_name
        self.dir_model_card = dir_model  # overridden in convert_lora_to_gguf.py

        # Apply heuristics to figure out typical tensor encoding based on first layer tensor encoding type
        if self.ftype == gguf.LlamaFileType.GUESSED:
            # NOTE: can't use field "torch_dtype" in config.json, because some finetunes lie.
            _, first_tensor = next(self.get_tensors())
            if first_tensor.dtype == torch.float16:
                logger.info(f"choosing --outtype f16 from first tensor type ({first_tensor.dtype})")
                self.ftype = gguf.LlamaFileType.MOSTLY_F16
            else:
                logger.info(f"choosing --outtype bf16 from first tensor type ({first_tensor.dtype})")
                self.ftype = gguf.LlamaFileType.MOSTLY_BF16

        # Configure GGUF Writer
        self.gguf_writer = gguf.GGUFWriter(path=None, arch=gguf.MODEL_ARCH_NAMES[self.model_arch], endianess=self.endianess, use_temp_file=self.use_temp_file,
                                           split_max_tensors=split_max_tensors, split_max_size=split_max_size, dry_run=dry_run, small_first_shard=small_first_shard)

    @classmethod
    def add_prefix_to_filename(cls, path: Path, prefix: str) -> Path:
        stem, suffix = path.stem, path.suffix
        new_name = f"{prefix}{stem}{suffix}"
        return path.with_name(new_name)

    def find_hparam(self, keys: Iterable[str], optional: bool = False) -> Any:
        key = next((k for k in keys if k in self.hparams), None)
        if key is not None:
            return self.hparams[key]
        if optional:
            return None
        raise KeyError(f"could not find any of: {keys}")

    def get_tensors(self) -> Iterator[tuple[str, Tensor]]:
        tensor_names_from_parts: set[str] = set()

        index_name = "model.safetensors" if self.is_safetensors else "pytorch_model.bin"
        index_name += ".index.json"
        index_file = self.dir_model / index_name

        if index_file.is_file():
            self.tensor_names = set()
            logger.info(f"gguf: loading model weight map from '{index_name}'")
            with open(index_file, "r", encoding="utf-8") as f:
                index: dict[str, Any] = json.load(f)
                weight_map = index.get("weight_map")
                if weight_map is None or not isinstance(weight_map, dict):
                    raise ValueError(f"Can't load 'weight_map' from {index_name!r}")
                self.tensor_names.update(weight_map.keys())
        else:
            self.tensor_names = tensor_names_from_parts
            weight_map = {}

        for part_name in self.part_names:
            logger.info(f"gguf: loading model part '{part_name}'")
            ctx: ContextManager[Any]
            if self.is_safetensors:
                from safetensors import safe_open
                ctx = cast(ContextManager[Any], safe_open(self.dir_model / part_name, framework="pt", device="cpu"))
            else:
                ctx = contextlib.nullcontext(torch.load(str(self.dir_model / part_name), map_location="cpu", mmap=True, weights_only=True))

            with ctx as model_part:
                tensor_names_from_parts.update(model_part.keys())

                for name in model_part.keys():
                    if self.is_safetensors:
                        if self.lazy:
                            data = model_part.get_slice(name)
                            data = LazyTorchTensor.from_safetensors_slice(data)
                        else:
                            data = model_part.get_tensor(name)
                    else:
                        data = model_part[name]
                        if self.lazy:
                            data = LazyTorchTensor.from_eager(data)
                    yield name, data

        # verify tensor name presence and identify potentially missing files
        if len(tensor_names_from_parts.symmetric_difference(self.tensor_names)) > 0:
            missing = sorted(self.tensor_names.difference(tensor_names_from_parts))
            extra = sorted(tensor_names_from_parts.difference(self.tensor_names))
            missing_files = sorted(set(weight_map[n] for n in missing if n in weight_map))
            if len(extra) == 0 and len(missing_files) > 0:
                raise ValueError(f"Missing or incomplete model files: {missing_files}\n"
                                 f"Missing tensors: {missing}")
            else:
                raise ValueError("Mismatch between weight map and model parts for tensor names:\n"
                                 f"Missing tensors: {missing}\n"
                                 f"Extra tensors: {extra}")

    def format_tensor_name(self, key: gguf.MODEL_TENSOR, bid: int | None = None, suffix: str = ".weight") -> str:
        if key not in gguf.MODEL_TENSORS[self.model_arch]:
            raise ValueError(f"Missing {key!r} for MODEL_TENSORS of {self.model_arch!r}")
        name: str = gguf.TENSOR_NAMES[key]
        if "{bid}" in name:
            assert bid is not None
            name = name.format(bid=bid)
        return name + suffix

    def match_model_tensor_name(self, name: str, key: gguf.MODEL_TENSOR, bid: int | None, suffix: str = ".weight") -> bool:
        if key not in gguf.MODEL_TENSORS[self.model_arch]:
            return False
        key_name: str = gguf.TENSOR_NAMES[key]
        if "{bid}" in key_name:
            if bid is None:
                return False
            key_name = key_name.format(bid=bid)
        else:
            if bid is not None:
                return False
        return name == (key_name + suffix)

    def map_tensor_name(self, name: str, try_suffixes: Sequence[str] = (".weight", ".bias")) -> str:
        new_name = self.tensor_map.get_name(key=name, try_suffixes=try_suffixes)
        if new_name is None:
            raise ValueError(f"Can not map tensor {name!r}")
        return new_name

    def set_gguf_parameters(self):
        raise NotImplementedError("set_gguf_parameters() must be implemented in subclasses")

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        return [(self.map_tensor_name(name), data_torch)]

    def tensor_force_quant(self, name: str, new_name: str, bid: int | None, n_dims: int) -> gguf.GGMLQuantizationType | bool:
        del name, new_name, bid, n_dims  # unused

        return False

    # some models need extra generated tensors (like rope_freqs)
    def generate_extra_tensors(self) -> Iterable[tuple[str, Tensor]]:
        return ()

    def prepare_tensors(self):
        max_name_len = max(len(s) for _, s in self.tensor_map.mapping.values()) + len(".weight,")

        for name, data_torch in chain(self.generate_extra_tensors(), self.get_tensors()):
            # we don't need these
            if name.endswith((".attention.masked_bias", ".attention.bias", ".rotary_emb.inv_freq")):
                continue

            old_dtype = data_torch.dtype

            # convert any unsupported data types to float32
            if data_torch.dtype not in (torch.float16, torch.float32):
                data_torch = data_torch.to(torch.float32)

            # use the first number-like part of the tensor name as the block id
            bid = None
            for part in name.split("."):
                if part.isdecimal():
                    bid = int(part)
                    break

            for new_name, data_torch in (self.modify_tensors(data_torch, name, bid)):
                # TODO: why do we squeeze here?
                # data = data_torch.squeeze().numpy()
                data = data_torch.numpy()

                # if data ends up empty, it means data_torch was a scalar tensor -> restore
                if len(data.shape) == 0:
                    data = data_torch.numpy()

                n_dims = len(data.shape)
                data_qtype: gguf.GGMLQuantizationType | bool = self.tensor_force_quant(name, new_name, bid, n_dims)

                # Most of the codebase that takes in 1D tensors or norms only handles F32 tensors
                if n_dims <= 1 or new_name.endswith("_norm.weight"):
                    data_qtype = gguf.GGMLQuantizationType.F32

                # Conditions should closely match those in llama_model_quantize_internal in llama.cpp
                # Some tensor types are always in float32
                if data_qtype is False and (
                    any(
                        self.match_model_tensor_name(new_name, key, bid)
                        for key in (
                            gguf.MODEL_TENSOR.FFN_GATE_INP,
                            gguf.MODEL_TENSOR.POS_EMBD,
                            gguf.MODEL_TENSOR.TOKEN_TYPES,
                            gguf.MODEL_TENSOR.SSM_CONV1D,
                            gguf.MODEL_TENSOR.SHORTCONV_CONV,
                            gguf.MODEL_TENSOR.TIME_MIX_FIRST,
                            gguf.MODEL_TENSOR.TIME_MIX_W1,
                            gguf.MODEL_TENSOR.TIME_MIX_W2,
                            gguf.MODEL_TENSOR.TIME_MIX_DECAY_W1,
                            gguf.MODEL_TENSOR.TIME_MIX_DECAY_W2,
                            gguf.MODEL_TENSOR.TIME_MIX_LERP_FUSED,
                            gguf.MODEL_TENSOR.POSNET_NORM1,
                            gguf.MODEL_TENSOR.POSNET_NORM2,
                            gguf.MODEL_TENSOR.V_ENC_EMBD_POS,
                            gguf.MODEL_TENSOR.A_ENC_EMBD_POS,
                            gguf.MODEL_TENSOR.ALTUP_CORRECT_COEF,
                            gguf.MODEL_TENSOR.ALTUP_PREDICT_COEF,
                        )
                    )
                    or not new_name.endswith(".weight")
                ):
                    data_qtype = gguf.GGMLQuantizationType.F32

                if data_qtype is False and any(
                    self.match_model_tensor_name(new_name, key, bid)
                    for key in (
                        gguf.MODEL_TENSOR.TOKEN_EMBD,
                        gguf.MODEL_TENSOR.PER_LAYER_TOKEN_EMBD,
                        gguf.MODEL_TENSOR.OUTPUT,
                        gguf.MODEL_TENSOR.ALTUP_ROUTER,
                        gguf.MODEL_TENSOR.LAUREL_L,
                        gguf.MODEL_TENSOR.LAUREL_R,
                    )
                ):
                    if self.ftype in (
                        gguf.LlamaFileType.MOSTLY_TQ1_0,
                        gguf.LlamaFileType.MOSTLY_TQ2_0,
                    ):
                        # TODO: use Q4_K and Q6_K
                        data_qtype = gguf.GGMLQuantizationType.F16

                # No override (data_qtype is False), or wants to be quantized (data_qtype is True)
                if isinstance(data_qtype, bool):
                    if self.ftype == gguf.LlamaFileType.ALL_F32:
                        data_qtype = gguf.GGMLQuantizationType.F32
                    elif self.ftype == gguf.LlamaFileType.MOSTLY_F16:
                        data_qtype = gguf.GGMLQuantizationType.F16
                    elif self.ftype == gguf.LlamaFileType.MOSTLY_BF16:
                        data_qtype = gguf.GGMLQuantizationType.BF16
                    elif self.ftype == gguf.LlamaFileType.MOSTLY_Q8_0:
                        data_qtype = gguf.GGMLQuantizationType.Q8_0
                    elif self.ftype == gguf.LlamaFileType.MOSTLY_TQ1_0:
                        data_qtype = gguf.GGMLQuantizationType.TQ1_0
                    elif self.ftype == gguf.LlamaFileType.MOSTLY_TQ2_0:
                        data_qtype = gguf.GGMLQuantizationType.TQ2_0
                    else:
                        raise ValueError(f"Unknown file type: {self.ftype.name}")

                try:
                    data = gguf.quants.quantize(data, data_qtype)
                except gguf.QuantError as e:
                    logger.warning("%s, %s", e, "falling back to F16")
                    data_qtype = gguf.GGMLQuantizationType.F16
                    data = gguf.quants.quantize(data, data_qtype)

                shape = gguf.quant_shape_from_byte_shape(data.shape, data_qtype) if data.dtype == np.uint8 else data.shape

                # reverse shape to make it similar to the internal ggml dimension order
                shape_str = f"{{{', '.join(str(n) for n in reversed(shape))}}}"

                # n_dims is implicit in the shape
                logger.info(f"{f'%-{max_name_len}s' % f'{new_name},'} {old_dtype} --> {data_qtype.name}, shape = {shape_str}")

                self.gguf_writer.add_tensor(new_name, data, raw_dtype=data_qtype)

    def set_type(self):
        self.gguf_writer.add_type(gguf.GGUFType.MODEL)

    def prepare_metadata(self, vocab_only: bool):

        total_params, shared_params, expert_params, expert_count = self.gguf_writer.get_total_parameter_count()

        self.metadata = gguf.Metadata.load(self.metadata_override, self.dir_model_card, self.model_name, total_params)

        # If we are using HF model id, set the metadata name to the model id
        if self.remote_hf_model_id:
            self.metadata.name = self.remote_hf_model_id

        # Fallback to model directory name if metadata name is still missing
        if self.metadata.name is None:
            self.metadata.name = self.dir_model.name

        # Generate parameter weight class (useful for leader boards) if not yet determined
        if self.metadata.size_label is None and total_params > 0:
            self.metadata.size_label = gguf.size_label(total_params, shared_params, expert_params, expert_count)

        self.set_type()

        logger.info("Set meta model")
        self.metadata.set_gguf_meta_model(self.gguf_writer)

        logger.info("Set model parameters")
        self.set_gguf_parameters()

        logger.info("Set model quantization version")
        self.gguf_writer.add_quantization_version(gguf.GGML_QUANT_VERSION)

    def write_vocab(self):
        raise NotImplementedError("write_vocab() must be implemented in subclasses")

    def write(self):
        self.prepare_tensors()
        self.prepare_metadata(vocab_only=False)
        self.gguf_writer.write_header_to_file(path=self.fname_out)
        self.gguf_writer.write_kv_data_to_file()
        self.gguf_writer.write_tensors_to_file(progress=True)
        self.gguf_writer.close()

    @staticmethod
    def get_model_part_names(dir_model: Path, prefix: str, suffix: str) -> list[str]:
        part_names: list[str] = []
        for filename in os.listdir(dir_model):
            if filename.startswith(prefix) and filename.endswith(suffix):
                part_names.append(filename)

        part_names.sort()

        return part_names

    @staticmethod
    def load_hparams(dir_model: Path):
        try:
            # for security reason, we don't allow loading remote code by default
            # if a model need remote code, we will fallback to config.json
            config = AutoConfig.from_pretrained(dir_model, trust_remote_code=False).to_dict()
        except Exception as e:
            logger.warning(f"Failed to load model config from {dir_model}: {e}")
            logger.warning("Trying to load config.json instead")
            with open(dir_model / "config.json", "r", encoding="utf-8") as f:
                config = json.load(f)
        if "llm_config" in config:
            # rename for InternVL
            config["text_config"] = config["llm_config"]
        if "thinker_config" in config:
            # rename for Qwen2.5-Omni
            config["text_config"] = config["thinker_config"]["text_config"]
        return config

    @classmethod
    def register(cls, *names: str) -> Callable[[AnyModel], AnyModel]:
        assert names

        def func(modelcls: AnyModel) -> AnyModel:
            model_type = ModelType.MMPROJ if modelcls.model_arch == gguf.MODEL_ARCH.MMPROJ else ModelType.TEXT
            for name in names:
                cls._model_classes[model_type][name] = modelcls
            return modelcls
        return func

    @classmethod
    def print_registered_models(cls):
        for model_type, model_classes in cls._model_classes.items():
            logger.error(f"{model_type.name} models:")
            for name in sorted(model_classes.keys()):
                logger.error(f"  - {name}")

    @classmethod
    def from_model_architecture(cls, arch: str, model_type = ModelType.TEXT) -> type[ModelBase]:
        try:
            return cls._model_classes[model_type][arch]
        except KeyError:
            raise NotImplementedError(f'Architecture {arch!r} not supported!') from None


class TextModel(ModelBase):
    model_type = ModelType.TEXT
    hf_arch: str

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hf_arch = get_model_architecture(self.hparams, self.model_type)

        if "text_config" in self.hparams:
            # move the text_config to the root level
            self.hparams = {**self.hparams, **self.hparams["text_config"]}

        self.block_count = self.find_hparam(["n_layers", "num_hidden_layers", "n_layer", "num_layers"])
        self.tensor_map = gguf.get_tensor_name_map(self.model_arch, self.block_count)

    @classmethod
    def __init_subclass__(cls):
        # can't use an abstract property, because overriding it without type errors
        # would require using decorated functions instead of simply defining the property
        if "model_arch" not in cls.__dict__:
            raise TypeError(f"Missing property 'model_arch' for {cls.__name__!r}")

    def set_vocab(self):
        self._set_vocab_gpt2()

    def prepare_metadata(self, vocab_only: bool):
        super().prepare_metadata(vocab_only=vocab_only)

        total_params = self.gguf_writer.get_total_parameter_count()[0]
        # Extract the encoding scheme from the file type name. e.g. 'gguf.LlamaFileType.MOSTLY_Q8_0' --> 'Q8_0'
        output_type: str = self.ftype.name.partition("_")[2]

        # Filename Output
        if self.fname_out.is_dir():
            # Generate default filename based on model specification and available metadata
            if not vocab_only:
                fname_default: str = gguf.naming_convention(self.metadata.name, self.metadata.basename, self.metadata.finetune, self.metadata.version, self.metadata.size_label, output_type, model_type="LoRA" if total_params < 0 else None)
            else:
                fname_default: str = gguf.naming_convention(self.metadata.name, self.metadata.basename, self.metadata.finetune, self.metadata.version, size_label=None, output_type=None, model_type="vocab")

            # Use the default filename
            self.fname_out = self.fname_out / f"{fname_default}.gguf"
        else:
            # Output path is a custom defined templated filename
            # Note: `not is_dir()` is used because `.is_file()` will not detect
            #       file template strings as it doesn't actually exist as a file

            # Process templated file name with the output ftype, useful with the "auto" ftype
            self.fname_out = self.fname_out.parent / gguf.fill_templated_filename(self.fname_out.name, output_type)

        logger.info("Set model tokenizer")
        self.set_vocab()

    def set_gguf_parameters(self):
        self.gguf_writer.add_block_count(self.block_count)

        if (n_ctx := self.find_hparam(["max_position_embeddings", "n_ctx", "n_positions", "max_length"], optional=True)) is not None:
            self.gguf_writer.add_context_length(n_ctx)
            logger.info(f"gguf: context length = {n_ctx}")

        if (n_embd := self.find_hparam(["hidden_size", "n_embd", "dim"], optional=True)) is not None:
            self.gguf_writer.add_embedding_length(n_embd)
            logger.info(f"gguf: embedding length = {n_embd}")

        if (n_ff := self.find_hparam(["intermediate_size", "n_inner", "hidden_dim"], optional=True)) is not None:
            self.gguf_writer.add_feed_forward_length(n_ff)
            logger.info(f"gguf: feed forward length = {n_ff}")

        if (n_head := self.find_hparam(["num_attention_heads", "n_head", "n_heads"], optional=True)) is not None:
            self.gguf_writer.add_head_count(n_head)
            logger.info(f"gguf: head count = {n_head}")

        if (n_head_kv := self.hparams.get("num_key_value_heads")) is not None:
            self.gguf_writer.add_head_count_kv(n_head_kv)
            logger.info(f"gguf: key-value head count = {n_head_kv}")

        if (rope_theta := self.hparams.get("rope_theta")) is not None:
            self.gguf_writer.add_rope_freq_base(rope_theta)
            logger.info(f"gguf: rope theta = {rope_theta}")
        if (f_rms_eps := self.hparams.get("rms_norm_eps")) is not None:
            self.gguf_writer.add_layer_norm_rms_eps(f_rms_eps)
            logger.info(f"gguf: rms norm epsilon = {f_rms_eps}")
        if (f_norm_eps := self.find_hparam(["layer_norm_eps", "layer_norm_epsilon", "norm_epsilon"], optional=True)) is not None:
            self.gguf_writer.add_layer_norm_eps(f_norm_eps)
            logger.info(f"gguf: layer norm epsilon = {f_norm_eps}")
        if (n_experts := self.hparams.get("num_local_experts")) is not None:
            self.gguf_writer.add_expert_count(n_experts)
            logger.info(f"gguf: expert count = {n_experts}")
        if (n_experts_used := self.hparams.get("num_experts_per_tok")) is not None:
            self.gguf_writer.add_expert_used_count(n_experts_used)
            logger.info(f"gguf: experts used count = {n_experts_used}")

        if (head_dim := self.hparams.get("head_dim")) is not None:
            self.gguf_writer.add_key_length(head_dim)
            self.gguf_writer.add_value_length(head_dim)

        self.gguf_writer.add_file_type(self.ftype)
        logger.info(f"gguf: file type = {self.ftype}")

    def write_vocab(self):
        if len(self.gguf_writer.tensors) != 1:
            raise ValueError('Splitting the vocabulary is not supported')

        self.prepare_metadata(vocab_only=True)
        self.gguf_writer.write_header_to_file(path=self.fname_out)
        self.gguf_writer.write_kv_data_to_file()
        self.gguf_writer.close()

    def does_token_look_special(self, token: str | bytes) -> bool:
        if isinstance(token, (bytes, bytearray)):
            token_text = token.decode(encoding="utf-8")
        elif isinstance(token, memoryview):
            token_text = token.tobytes().decode(encoding="utf-8")
        else:
            token_text = token

        # Some models mark some added tokens which ought to be control tokens as not special.
        # (e.g. command-r, command-r-plus, deepseek-coder, gemma{,-2})
        seems_special = token_text in (
            "<pad>",  # deepseek-coder
            "<mask>", "<2mass>", "[@BOS@]",  # gemma{,-2}
        )

        seems_special = seems_special or (token_text.startswith("<|") and token_text.endswith("|>"))
        seems_special = seems_special or (token_text.startswith("<｜") and token_text.endswith("｜>"))  # deepseek-coder

        # TODO: should these be marked as UNUSED instead? (maybe not)
        seems_special = seems_special or (token_text.startswith("<unused") and token_text.endswith(">"))  # gemma{,-2}

        return seems_special

    # used for GPT-2 BPE and WordPiece vocabs
    def get_vocab_base(self) -> tuple[list[str], list[int], str]:
        tokens: list[str] = []
        toktypes: list[int] = []

        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(self.dir_model)
        vocab_size = self.hparams.get("vocab_size", len(tokenizer.vocab))
        assert max(tokenizer.vocab.values()) < vocab_size

        tokpre = self.get_vocab_base_pre(tokenizer)

        reverse_vocab = {id_: encoded_tok for encoded_tok, id_ in tokenizer.vocab.items()}
        added_vocab = tokenizer.get_added_vocab()

        added_tokens_decoder = tokenizer.added_tokens_decoder

        for i in range(vocab_size):
            if i not in reverse_vocab:
                tokens.append(f"[PAD{i}]")
                toktypes.append(gguf.TokenType.UNUSED)
            else:
                token: str = reverse_vocab[i]
                if token in added_vocab:
                    # The tokenizer in llama.cpp assumes the CONTROL and USER_DEFINED tokens are pre-normalized.
                    # To avoid unexpected issues - we make sure to normalize non-normalized tokens
                    if not added_tokens_decoder[i].normalized:
                        previous_token = token
                        token = tokenizer.decode(tokenizer.encode(token, add_special_tokens=False))
                        if previous_token != token:
                            logger.info(f"{repr(previous_token)} is encoded and decoded back to {repr(token)} using AutoTokenizer")

                    if added_tokens_decoder[i].special or self.does_token_look_special(token):
                        toktypes.append(gguf.TokenType.CONTROL)
                    else:
                        # NOTE: this was added for Gemma.
                        # Encoding and decoding the tokens above isn't sufficient for this case.
                        token = token.replace(b"\xe2\x96\x81".decode("utf-8"), " ")  # pre-normalize user-defined spaces
                        toktypes.append(gguf.TokenType.USER_DEFINED)
                else:
                    toktypes.append(gguf.TokenType.NORMAL)
                tokens.append(token)

        return tokens, toktypes, tokpre

    # NOTE: this function is generated by convert_hf_to_gguf_update.py
    #       do not modify it manually!
    # ref:  https://github.com/ggml-org/llama.cpp/pull/6920
    # Marker: Start get_vocab_base_pre
    def get_vocab_base_pre(self, tokenizer) -> str:
        # encoding this string and hashing the resulting tokens would (hopefully) give us a unique identifier that
        # is specific for the BPE pre-tokenizer used by the model
        # we will use this unique identifier to write a "tokenizer.ggml.pre" entry in the GGUF file which we can
        # use in llama.cpp to implement the same pre-tokenizer

        chktxt = '\n \n\n \n\n\n \t \t\t \t\n  \n   \n    \n     \n🚀 (normal) 😶\u200d🌫️ (multiple emojis concatenated) ✅ 🦙🦙 3 33 333 3333 33333 333333 3333333 33333333 3.3 3..3 3...3 កាន់តែពិសេសអាច😁 ?我想在apple工作1314151天～ ------======= нещо на Български \'\'\'\'\'\'```````""""......!!!!!!?????? I\'ve been \'told he\'s there, \'RE you sure? \'M not sure I\'ll make it, \'D you like some tea? We\'Ve a\'lL'

        chktok = tokenizer.encode(chktxt)
        chkhsh = sha256(str(chktok).encode()).hexdigest()

        logger.debug(f"chktok: {chktok}")
        logger.debug(f"chkhsh: {chkhsh}")

        res = None

        # NOTE: if you get an error here, you need to update the convert_hf_to_gguf_update.py script
        #       or pull the latest version of the model from Huggingface
        #       don't edit the hashes manually!
        if chkhsh == "b6e8e1518dc4305be2fe39c313ed643381c4da5db34a98f6a04c093f8afbe99b":
            # ref: https://huggingface.co/THUDM/glm-4-9b-chat
            res = "chatglm-bpe"
        if chkhsh == "81d72c7348a9f0ebe86f23298d37debe0a5e71149e29bd283904c02262b27516":
            # ref: https://huggingface.co/THUDM/glm-4-9b-chat
            res = "chatglm-bpe"
        if chkhsh == "a1336059768a55c99a734006ffb02203cd450fed003e9a71886c88acf24fdbc2":
            # ref: https://huggingface.co/THUDM/glm-4-9b-hf
            res = "glm4"
        if chkhsh == "1431a23e583c97432bc230bff598d103ddb5a1f89960c8f1d1051aaa944d0b35":
            # ref: https://huggingface.co/sapienzanlp/Minerva-7B-base-v1.0
            res = "minerva-7b"
        if chkhsh == "7e57df22b1fe23a7b1e1c7f3dc4e3f96d43a4eb0836d0c6bdc3436d7b2f1c664":
            # ref: https://huggingface.co/tencent/Hunyuan-A13B-Instruct
            res = "hunyuan"
        if chkhsh == "a6b57017d60e6edb4d88ecc2845188e0eb333a70357e45dcc9b53964a73bbae6":
            # ref: https://huggingface.co/tiiuae/Falcon-H1-0.5B-Base
            res = "falcon-h1"
        if chkhsh == "60476e1243776c4fb1b993dbd7a5f15ac22f83c80afdf425fa5ae01c8d44ef86":
            # ref: https://huggingface.co/tiiuae/Falcon-H1-1B-Base
            res = "falcon-h1"
        if chkhsh == "3eda48b4c4dc7de733d1a8b3e3b4a85243dbbf704da2ee9d42c6beced8897896":
            # ref: https://huggingface.co/tiiuae/Falcon-H1-7B-Base
            res = "falcon-h1"
        if chkhsh == "48f8e02c0359c0bbdd82f26909171fac1c18a457bb47573ed1fe3bbb2c1cfd4b":
            # ref: https://huggingface.co/tiiuae/Falcon-H1-34B-Base
            res = "falcon-h1"
        if chkhsh == "81212dc7cdb7e0c1074ca62c5aeab0d43c9f52b8a737be7b12a777c953027890":
            # ref: https://huggingface.co/moonshotai/Kimi-K2-Base
            res = "kimi-k2"
        if chkhsh == "0ef9807a4087ebef797fc749390439009c3b9eda9ad1a097abbe738f486c01e5":
            # ref: https://huggingface.co/meta-llama/Meta-Llama-3-8B
            res = "llama-bpe"
        if chkhsh == "049ecf7629871e3041641907f3de7c733e4dbfdc736f57d882ba0b0845599754":
            # ref: https://huggingface.co/deepseek-ai/deepseek-llm-7b-base
            res = "deepseek-llm"
        if chkhsh == "347715f544604f9118bb75ed199f68779f423cabb20db6de6f31b908d04d7821":
            # ref: https://huggingface.co/deepseek-ai/deepseek-coder-6.7b-base
            res = "deepseek-coder"
        if chkhsh == "8aeee3860c56296a157a1fe2fad249ec40aa59b1bb5709f4ade11c4e6fe652ed":
            # ref: https://huggingface.co/tiiuae/falcon-7b
            res = "falcon"
        if chkhsh == "0876d13b50744004aa9aeae05e7b0647eac9d801b5ba4668afc01e709c15e19f":
            # ref: https://huggingface.co/BAAI/bge-small-en-v1.5
            res = "bert-bge"
        if chkhsh == "9d032fcbd5501f4a38150912590928bfb36091efb5df11b8e2124b0390e3fb1e":
            # ref: https://huggingface.co/tiiuae/Falcon3-7B-Base
            res = "falcon3"
        if chkhsh == "8e62295832751ca1e8f92f2226f403dea30dc5165e448b5bfa05af5340c64ec7":
            # ref: https://huggingface.co/BAAI/bge-large-zh-v1.5
            res = "bert-bge-large"
        if chkhsh == "b6dc8df998e1cfbdc4eac8243701a65afe638679230920b50d6f17d81c098166":
            # ref: https://huggingface.co/mosaicml/mpt-7b
            res = "mpt"
        if chkhsh == "35d91631860c815f952d711435f48d356ebac988362536bed955d43bfa436e34":
            # ref: https://huggingface.co/bigcode/starcoder2-3b
            res = "starcoder"
        if chkhsh == "3ce83efda5659b07b1ad37ca97ca5797ea4285d9b9ab0dc679e4a720c9da7454":
            # ref: https://huggingface.co/openai-community/gpt2
            res = "gpt-2"
        if chkhsh == "32d85c31273f8019248f2559fed492d929ea28b17e51d81d3bb36fff23ca72b3":
            # ref: https://huggingface.co/stabilityai/stablelm-2-zephyr-1_6b
            res = "stablelm2"
        if chkhsh == "6221ad2852e85ce96f791f476e0b390cf9b474c9e3d1362f53a24a06dc8220ff":
            # ref: https://huggingface.co/smallcloudai/Refact-1_6-base
            res = "refact"
        if chkhsh == "9c2227e4dd922002fb81bde4fc02b0483ca4f12911410dee2255e4987644e3f8":
            # ref: https://huggingface.co/CohereForAI/c4ai-command-r-v01
            res = "command-r"
        if chkhsh == "e636dc30a262dcc0d8c323492e32ae2b70728f4df7dfe9737d9f920a282b8aea":
            # ref: https://huggingface.co/Qwen/Qwen1.5-7B
            res = "qwen2"
        if chkhsh == "b6dc8df998e1cfbdc4eac8243701a65afe638679230920b50d6f17d81c098166":
            # ref: https://huggingface.co/allenai/OLMo-1.7-7B-hf
            res = "olmo"
        if chkhsh == "a8594e3edff7c29c003940395316294b2c623e09894deebbc65f33f1515df79e":
            # ref: https://huggingface.co/databricks/dbrx-base
            res = "dbrx"
        if chkhsh == "c7699093ba4255a91e702aa38a596aa81669f3525dae06c2953267dde580f448":
            # ref: https://huggingface.co/jinaai/jina-reranker-v1-tiny-en
            res = "jina-v1-en"
        if chkhsh == "0876d13b50744004aa9aeae05e7b0647eac9d801b5ba4668afc01e709c15e19f":
            # ref: https://huggingface.co/jinaai/jina-embeddings-v2-base-en
            res = "jina-v2-en"
        if chkhsh == "171aeeedd6fb548d418a7461d053f11b6f1f1fc9b387bd66640d28a4b9f5c643":
            # ref: https://huggingface.co/jinaai/jina-embeddings-v2-base-es
            res = "jina-v2-es"
        if chkhsh == "27949a2493fc4a9f53f5b9b029c82689cfbe5d3a1929bb25e043089e28466de6":
            # ref: https://huggingface.co/jinaai/jina-embeddings-v2-base-de
            res = "jina-v2-de"
        if chkhsh == "c136ed14d01c2745d4f60a9596ae66800e2b61fa45643e72436041855ad4089d":
            # ref: https://huggingface.co/abacusai/Smaug-Llama-3-70B-Instruct
            res = "smaug-bpe"
        if chkhsh == "c7ea5862a53e4272c035c8238367063e2b270d51faa48c0f09e9d5b54746c360":
            # ref: https://huggingface.co/LumiOpen/Poro-34B-chat
            res = "poro-chat"
        if chkhsh == "7967bfa498ade6b757b064f31e964dddbb80f8f9a4d68d4ba7998fcf281c531a":
            # ref: https://huggingface.co/jinaai/jina-embeddings-v2-base-code
            res = "jina-v2-code"
        if chkhsh == "7fc505bd3104ca1083b150b17d088b59534ede9bde81f0dd2090967d7fe52cee":
            # ref: https://huggingface.co/LumiOpen/Viking-7B
            res = "viking"
        if chkhsh == "b53802fb28e26d645c3a310b34bfe07da813026ec7c7716883404d5e0f8b1901":
            # ref: https://huggingface.co/core42/jais-13b
            res = "jais"
        if chkhsh == "7b3e7548e4308f52a76e8229e4e6cc831195d0d1df43aed21ac6c93da05fec5f":
            # ref: https://huggingface.co/WisdomShell/CodeShell-7B
            res = "codeshell"
        if chkhsh == "63b97e4253352e6f357cc59ea5b583e3a680eaeaf2632188c2b952de2588485e":
            # ref: https://huggingface.co/mistralai/Mistral-Nemo-Base-2407
            res = "tekken"
        if chkhsh == "855059429035d75a914d1eda9f10a876752e281a054a7a3d421ef0533e5b6249":
            # ref: https://huggingface.co/HuggingFaceTB/SmolLM-135M
            res = "smollm"
        if chkhsh == "3c30d3ad1d6b64202cd222813e7736c2db6e1bd6d67197090fc1211fbc612ae7":
            # ref: https://huggingface.co/bigscience/bloom
            res = "bloom"
        if chkhsh == "bc01ce58980e1db43859146dc51b1758b3b88729b217a74792e9f8d43e479d21":
            # ref: https://huggingface.co/TurkuNLP/gpt3-finnish-small
            res = "gpt3-finnish"
        if chkhsh == "4e2b24cc4770243d65a2c9ec19770a72f08cffc161adbb73fcbb6b7dd45a0aae":
            # ref: https://huggingface.co/LGAI-EXAONE/EXAONE-3.0-7.8B-Instruct
            res = "exaone"
        if chkhsh == "fcace8b9cac38ce847670c970cd5892031a753a1ef381abd1d9af00f713da085":
            # ref: https://huggingface.co/microsoft/phi-2
            res = "phi-2"
        if chkhsh == "60824e3c0d9401f89943cbb2fff727f0e2d4c545ba4df2d6e4f09a6db0f5b450":
            # ref: https://huggingface.co/facebook/chameleon-7b
            res = "chameleon"
        if chkhsh == "8b5a93ed704057481f240da0be7e7dca721d7f8f4755263b6807227a2cbeae65":
            # ref: https://huggingface.co/sentence-transformers/stsb-roberta-base
            res = "roberta-bpe"
        if chkhsh == "ad851be1dba641f2e3711822f816db2c265f788b37c63b4e1aeacb9ee92de8eb":
            # ref: https://huggingface.co/ai-sage/GigaChat-20B-A3B-instruct
            res = "gigachat"
        if chkhsh == "d4c8f286ea6b520b3d495c4455483cfa2302c0cfcd4be05d781b6a8a0a7cdaf1":
            # ref: https://huggingface.co/Infinigence/Megrez-3B-Instruct
            res = "megrez"
        if chkhsh == "877081d19cf6996e2c4ff0e1236341e9b7bde288f5311a56a937f0afbbb3aeb5":
            # ref: https://huggingface.co/deepseek-ai/DeepSeek-V3
            res = "deepseek-v3"
        if chkhsh == "b3f499bb4255f8ca19fccd664443283318f2fd2414d5e0b040fbdd0cc195d6c5":
            # ref: https://huggingface.co/deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B
            res = "deepseek-r1-qwen"
        if chkhsh == "ccc2ef013c104be7bae2965776d611e1d7a8a2a9c547dd93a682c9a9fc80352e":
            # ref: https://huggingface.co/Xenova/gpt-4o
            res = "gpt-4o"
        if chkhsh == "7dec86086fcc38b66b7bc1575a160ae21cf705be7718b9d5598190d7c12db76f":
            # ref: https://huggingface.co/UW/OLMo2-8B-SuperBPE-t180k
            res = "superbpe"
        if chkhsh == "1994ffd01900cfb37395608534236ecd63f2bd5995d6cb1004dda1af50240f15":
            # ref: https://huggingface.co/trillionlabs/Trillion-7B-preview
            res = "trillion"
        if chkhsh == "96a5f08be6259352137b512d4157e333e21df7edd3fcd152990608735a65b224":
            # ref: https://huggingface.co/inclusionAI/Ling-lite
            res = "bailingmoe"
        if chkhsh == "d353350c764d8c3b39c763113960e4fb4919bea5fbf208a0e3b22e8469dc7406":
            # ref: https://huggingface.co/meta-llama/Llama-4-Scout-17B-16E-Instruct
            res = "llama4"
        if chkhsh == "0e9433cbbb161f89e264eb32e8e64bfe69e834973ffca5d41d3948a604a3e2a3":
            # ref: https://huggingface.co/mistral-community/pixtral-12b
            res = "pixtral"
        if chkhsh == "d5f1dd6f980fec569fb218a81a7658ac45fc56b38c5a0adeb1c232fbe04ef5ec":
            # ref: https://huggingface.co/ByteDance-Seed/Seed-Coder-8B-Base
            res = "seed-coder"
        if chkhsh == "b0a6b1c0bd5998ebd9df08611efde34a4ff03faed45ae09c43e6b31ebd4b94cf":
            # ref: https://huggingface.co/skt/A.X-4.0
            res = "a.x-4.0"
        if chkhsh == "f6791d196f87ce6b56a7d234be618e0d58f8cda3549416635b2bebcd22cd95c4":
            # ref: https://huggingface.co/K-intelligence/Midm-2.0-Base-Instruct
            res = "midm-2.0"
        if chkhsh == "169bf0296a13c4d9b7672313f749eb36501d931022de052aad6e36f2bf34dd51":
            # ref: https://huggingface.co/LiquidAI/LFM2-Tokenizer
            res = "lfm2"
        if chkhsh == "2085e1638f6c377a0aa4ead21b27bb4cb941bf800df86ed391011769c1758dfb":
            # ref: https://huggingface.co/LGAI-EXAONE/EXAONE-4.0-32B
            res = "exaone4"

        if res is None:
            logger.warning("\n")
            logger.warning("**************************************************************************************")
            logger.warning("** WARNING: The BPE pre-tokenizer was not recognized!")
            logger.warning("**          There are 2 possible reasons for this:")
            logger.warning("**          - the model has not been added to convert_hf_to_gguf_update.py yet")
            logger.warning("**          - the pre-tokenization config has changed upstream")
            logger.warning("**          Check your model files and convert_hf_to_gguf_update.py and update them accordingly.")
            logger.warning("** ref:     https://github.com/ggml-org/llama.cpp/pull/6920")
            logger.warning("**")
            logger.warning(f"** chkhsh:  {chkhsh}")
            logger.warning("**************************************************************************************")
            logger.warning("\n")
            raise NotImplementedError("BPE pre-tokenizer was not recognized - update get_vocab_base_pre()")

        logger.debug(f"tokenizer.ggml.pre: {repr(res)}")
        logger.debug(f"chkhsh: {chkhsh}")

        return res
        # Marker: End get_vocab_base_pre

    def _set_vocab_none(self) -> None:
        self.gguf_writer.add_tokenizer_model("none")

    def _set_vocab_gpt2(self) -> None:
        tokens, toktypes, tokpre = self.get_vocab_base()
        self.gguf_writer.add_tokenizer_model("gpt2")
        self.gguf_writer.add_tokenizer_pre(tokpre)
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_types(toktypes)

        special_vocab = gguf.SpecialVocab(self.dir_model, load_merges=True)
        special_vocab.add_to_gguf(self.gguf_writer)

    def _set_vocab_qwen(self):
        dir_model = self.dir_model
        hparams = self.hparams
        tokens: list[str] = []
        toktypes: list[int] = []

        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(dir_model, trust_remote_code=True)
        vocab_size = hparams["vocab_size"]
        assert max(tokenizer.get_vocab().values()) < vocab_size

        tokpre = self.get_vocab_base_pre(tokenizer)

        merges = []
        vocab = {}
        mergeable_ranks = tokenizer.mergeable_ranks
        for token, rank in mergeable_ranks.items():
            vocab[QwenModel.token_bytes_to_string(token)] = rank
            if len(token) == 1:
                continue
            merged = QwenModel.bpe(mergeable_ranks, token, max_rank=rank)
            assert len(merged) == 2
            merges.append(' '.join(map(QwenModel.token_bytes_to_string, merged)))

        # for this kind of tokenizer, added_vocab is not a subset of vocab, so they need to be combined
        added_vocab = tokenizer.special_tokens
        reverse_vocab = {id_ : encoded_tok for encoded_tok, id_ in {**vocab, **added_vocab}.items()}

        for i in range(vocab_size):
            if i not in reverse_vocab:
                tokens.append(f"[PAD{i}]")
                toktypes.append(gguf.TokenType.UNUSED)
            elif reverse_vocab[i] in added_vocab:
                tokens.append(reverse_vocab[i])
                toktypes.append(gguf.TokenType.CONTROL)
            else:
                tokens.append(reverse_vocab[i])
                toktypes.append(gguf.TokenType.NORMAL)

        self.gguf_writer.add_tokenizer_model("gpt2")
        self.gguf_writer.add_tokenizer_pre(tokpre)
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_types(toktypes)

        special_vocab = gguf.SpecialVocab(dir_model, load_merges=False)
        special_vocab.merges = merges
        # only add special tokens when they were not already loaded from config.json
        if len(special_vocab.special_token_ids) == 0:
            special_vocab._set_special_token("bos", tokenizer.special_tokens["<|endoftext|>"])
            special_vocab._set_special_token("eos", tokenizer.special_tokens["<|endoftext|>"])
        # this one is usually not in config.json anyway
        special_vocab._set_special_token("unk", tokenizer.special_tokens["<|endoftext|>"])
        special_vocab.add_to_gguf(self.gguf_writer)

    def _set_vocab_sentencepiece(self, add_to_gguf=True):
        tokens, scores, toktypes = self._create_vocab_sentencepiece()

        self.gguf_writer.add_tokenizer_model("llama")
        self.gguf_writer.add_tokenizer_pre("default")
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_scores(scores)
        self.gguf_writer.add_token_types(toktypes)

        special_vocab = gguf.SpecialVocab(self.dir_model, n_vocab=len(tokens))
        special_vocab.add_to_gguf(self.gguf_writer)

    def _create_vocab_sentencepiece(self):
        from sentencepiece import SentencePieceProcessor

        tokenizer_path = self.dir_model / 'tokenizer.model'

        if not tokenizer_path.is_file():
            raise FileNotFoundError(f"File not found: {tokenizer_path}")

        tokenizer = SentencePieceProcessor()
        tokenizer.LoadFromFile(str(tokenizer_path))

        vocab_size = self.find_hparam([
            "vocab_size_per_layer_input", # gemma3n
            "vocab_size",
        ], optional=True) or tokenizer.vocab_size()

        tokens: list[bytes] = [f"[PAD{i}]".encode("utf-8") for i in range(vocab_size)]
        scores: list[float] = [-10000.0] * vocab_size
        toktypes: list[int] = [SentencePieceTokenTypes.UNUSED] * vocab_size

        for token_id in range(tokenizer.vocab_size()):
            if token_id >= vocab_size:
                logger.warning(f'ignore tokens from {token_id}: id is out of range, max={vocab_size - 1}')
                break

            piece = tokenizer.IdToPiece(token_id)
            text = piece.encode("utf-8")
            score = tokenizer.GetScore(token_id)

            toktype = SentencePieceTokenTypes.NORMAL
            if tokenizer.IsUnknown(token_id):
                toktype = SentencePieceTokenTypes.UNKNOWN
            elif tokenizer.IsControl(token_id):
                toktype = SentencePieceTokenTypes.CONTROL
            elif tokenizer.IsUnused(token_id):
                toktype = SentencePieceTokenTypes.UNUSED
            elif tokenizer.IsByte(token_id):
                toktype = SentencePieceTokenTypes.BYTE

            tokens[token_id] = text
            scores[token_id] = score
            toktypes[token_id] = toktype

        added_tokens_file = self.dir_model / 'added_tokens.json'
        if added_tokens_file.is_file():
            with open(added_tokens_file, "r", encoding="utf-8") as f:
                added_tokens_json = json.load(f)
                for key in added_tokens_json:
                    token_id = added_tokens_json[key]
                    if token_id >= vocab_size:
                        logger.warning(f'ignore token {token_id}: id is out of range, max={vocab_size - 1}')
                        continue

                    tokens[token_id] = key.encode("utf-8")
                    scores[token_id] = -1000.0
                    toktypes[token_id] = SentencePieceTokenTypes.USER_DEFINED

        tokenizer_config_file = self.dir_model / 'tokenizer_config.json'
        if tokenizer_config_file.is_file():
            with open(tokenizer_config_file, "r", encoding="utf-8") as f:
                tokenizer_config_json = json.load(f)
                added_tokens_decoder = tokenizer_config_json.get("added_tokens_decoder", {})
                for token_id, token_data in added_tokens_decoder.items():
                    token_id = int(token_id)
                    token: str = token_data["content"]
                    if token_id >= vocab_size:
                        logger.warning(f'ignore token {token_id}: id is out of range, max={vocab_size - 1}')
                        continue
                    if toktypes[token_id] != SentencePieceTokenTypes.UNUSED:
                        if tokens[token_id] != token.encode("utf-8"):
                            logger.warning(f'replacing token {token_id}: {tokens[token_id].decode("utf-8")!r} -> {token!r}')
                    if token_data.get("special") or self.does_token_look_special(token):
                        toktypes[token_id] = SentencePieceTokenTypes.CONTROL
                    else:
                        token = token.replace(b"\xe2\x96\x81".decode("utf-8"), " ")  # pre-normalize user-defined spaces
                        toktypes[token_id] = SentencePieceTokenTypes.USER_DEFINED

                    scores[token_id] = -1000.0
                    tokens[token_id] = token.encode("utf-8")

        if vocab_size > len(tokens):
            pad_count = vocab_size - len(tokens)
            logger.debug(f"Padding vocab with {pad_count} token(s) - [PAD1] through [PAD{pad_count}]")
            for i in range(1, pad_count + 1):
                tokens.append(bytes(f"[PAD{i}]", encoding="utf-8"))
                scores.append(-1000.0)
                toktypes.append(SentencePieceTokenTypes.UNUSED)

        return tokens, scores, toktypes

    def _set_vocab_llama_hf(self):
        vocab = gguf.LlamaHfVocab(self.dir_model)
        tokens = []
        scores = []
        toktypes = []

        for text, score, toktype in vocab.all_tokens():
            tokens.append(text)
            scores.append(score)
            toktypes.append(toktype)

        assert len(tokens) == vocab.vocab_size

        self.gguf_writer.add_tokenizer_model("llama")
        self.gguf_writer.add_tokenizer_pre("default")
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_scores(scores)
        self.gguf_writer.add_token_types(toktypes)

        special_vocab = gguf.SpecialVocab(self.dir_model, n_vocab=len(tokens))
        special_vocab.add_to_gguf(self.gguf_writer)

    def _set_vocab_rwkv_world(self):
        assert (self.dir_model / "rwkv_vocab_v20230424.txt").is_file()
        vocab_size = self.hparams.get("vocab_size", 65536)

        tokens: list[bytes] = ['<s>'.encode("utf-8")]
        toktypes: list[int] = [gguf.TokenType.CONTROL]

        with open(self.dir_model / "rwkv_vocab_v20230424.txt", "r", encoding="utf-8") as f:
            lines = f.readlines()
            for line in lines:
                parts = line.split(' ')
                assert len(parts) >= 3
                token, token_len = ast.literal_eval(' '.join(parts[1:-1])), int(parts[-1])
                token = token.encode("utf-8") if isinstance(token, str) else token
                assert isinstance(token, bytes)
                assert len(token) == token_len
                token_text: str = repr(token)[2:-1]  # "b'\xff'" -> "\xff"
                tokens.append(token_text.encode("utf-8"))
                toktypes.append(gguf.TokenType.NORMAL)
        remainder = vocab_size - len(tokens)
        assert remainder >= 0
        for i in range(len(tokens), vocab_size):
            tokens.append(f"[PAD{i}]".encode("utf-8"))
            toktypes.append(gguf.TokenType.UNUSED)

        self.gguf_writer.add_tokenizer_model("rwkv")
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_types(toktypes)
        special_vocab = gguf.SpecialVocab(self.dir_model, load_merges=False)
        if special_vocab.chat_template is None:
            template_path = Path(__file__).parent / "models" / "templates" / "llama-cpp-rwkv-world.jinja"
            if template_path.is_file():
                with open(template_path, "r", encoding="utf-8") as f:
                    template = f.read()
            else:
                template = "rwkv-world"
            special_vocab.chat_template = template
        # hack: Add '\n\n' as the EOT token to make it chat normally
        special_vocab._set_special_token("eot", 261)
        # hack: Override these as they have already been set (incorrectly)
        special_vocab.special_token_ids["bos"] = 0
        special_vocab.special_token_ids["eos"] = 0

        special_vocab.add_to_gguf(self.gguf_writer)

    def _set_vocab_builtin(self, model_name: Literal["gpt-neox", "llama-spm"], vocab_size: int):
        tokenizer_path = Path(sys.path[0]) / "models" / f"ggml-vocab-{model_name}.gguf"
        logger.warning(f"Using tokenizer from '{os.path.relpath(tokenizer_path, os.getcwd())}'")
        vocab_reader = gguf.GGUFReader(tokenizer_path, "r")

        default_pre = "mpt" if model_name == "gpt-neox" else "default"

        field = vocab_reader.get_field(gguf.Keys.Tokenizer.MODEL)
        assert field  # tokenizer model
        self.gguf_writer.add_tokenizer_model(bytes(field.parts[-1]).decode("utf-8"))

        field = vocab_reader.get_field(gguf.Keys.Tokenizer.PRE)
        self.gguf_writer.add_tokenizer_pre(bytes(field.parts[-1]).decode("utf-8") if field else default_pre)

        field = vocab_reader.get_field(gguf.Keys.Tokenizer.LIST)
        assert field  # token list
        self.gguf_writer.add_token_list([bytes(field.parts[i]) for i in field.data][:vocab_size])

        if model_name == "llama-spm":
            field = vocab_reader.get_field(gguf.Keys.Tokenizer.SCORES)
            assert field  # token scores
            self.gguf_writer.add_token_scores([field.parts[i].tolist()[0] for i in field.data][:vocab_size])

        field = vocab_reader.get_field(gguf.Keys.Tokenizer.TOKEN_TYPE)
        assert field  # token types
        self.gguf_writer.add_token_types([field.parts[i].tolist()[0] for i in field.data][:vocab_size])

        if model_name != "llama-spm":
            field = vocab_reader.get_field(gguf.Keys.Tokenizer.MERGES)
            assert field  # token merges
            self.gguf_writer.add_token_merges([bytes(field.parts[i]) for i in field.data])

        if (field := vocab_reader.get_field(gguf.Keys.Tokenizer.BOS_ID)) is not None:
            self.gguf_writer.add_bos_token_id(field.parts[-1].tolist()[0])
        if (field := vocab_reader.get_field(gguf.Keys.Tokenizer.EOS_ID)) is not None:
            self.gguf_writer.add_eos_token_id(field.parts[-1].tolist()[0])
        if (field := vocab_reader.get_field(gguf.Keys.Tokenizer.UNK_ID)) is not None:
            self.gguf_writer.add_unk_token_id(field.parts[-1].tolist()[0])
        if (field := vocab_reader.get_field(gguf.Keys.Tokenizer.PAD_ID)) is not None:
            self.gguf_writer.add_pad_token_id(field.parts[-1].tolist()[0])
        if (field := vocab_reader.get_field(gguf.Keys.Tokenizer.ADD_BOS)) is not None:
            self.gguf_writer.add_add_bos_token(field.parts[-1].tolist()[0])
        if (field := vocab_reader.get_field(gguf.Keys.Tokenizer.ADD_EOS)) is not None:
            self.gguf_writer.add_add_eos_token(field.parts[-1].tolist()[0])

    def _try_set_pooling_type(self) -> None:
        # get pooling path
        pooling_path = None
        module_path = self.dir_model / "modules.json"
        if module_path.is_file():
            with open(module_path, encoding="utf-8") as f:
                modules = json.load(f)
            for mod in modules:
                if mod["type"] == "sentence_transformers.models.Pooling":
                    pooling_path = mod["path"]
                    break

        # get pooling type
        if pooling_path is not None:
            with open(self.dir_model / pooling_path / "config.json", encoding="utf-8") as f:
                pooling = json.load(f)
            if pooling["pooling_mode_mean_tokens"]:
                pooling_type = gguf.PoolingType.MEAN
            elif pooling["pooling_mode_cls_token"]:
                pooling_type = gguf.PoolingType.CLS
            elif pooling["pooling_mode_lasttoken"]:
                pooling_type = gguf.PoolingType.LAST
            else:
                raise NotImplementedError("Only MEAN, CLS, and LAST pooling types supported")
            self.gguf_writer.add_pooling_type(pooling_type)


class MmprojModel(ModelBase):
    model_type = ModelType.MMPROJ
    model_arch = gguf.MODEL_ARCH.MMPROJ
    preprocessor_config: dict[str, Any]
    global_config: dict[str, Any]

    n_block_keys = ["n_layers", "num_hidden_layers", "n_layer", "num_layers", "depth"]

    has_vision_encoder: bool = True # by default
    has_audio_encoder: bool = False

    # for models having multiple encoders, we need to separate their hparams
    hparams_vision: dict[str, Any] | None = None
    hparams_audio: dict[str, Any] | None = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.model_arch != gguf.MODEL_ARCH.MMPROJ:
            raise TypeError("MmprojModel must be subclassed with model_arch = gguf.MODEL_ARCH.MMPROJ")

        # get n_embd of the text model
        if "text_config" not in self.hparams:
            self.hparams["text_config"] = {}
        if "audio_config" not in self.hparams:
            self.hparams["audio_config"] = {}
        text_config = {**self.hparams, **self.hparams["text_config"]}
        self.n_embd_text = text_config.get("hidden_size", text_config.get("n_embd", 0))
        assert self.n_embd_text > 0, "n_embd not found in hparams"

        # move vision config to the top level, while preserving the original hparams in global_config
        import copy
        self.global_config = copy.deepcopy(self.hparams)
        self.hparams_vision = self.get_vision_config()
        self.hparams_audio = self.get_audio_config()

        if self.hparams_vision is None and self.hparams_audio is None:
            raise ValueError("vision_config / audio_config not found in hparams")

        # for compat with vision-only models
        self.hparams = self.hparams_vision or self.hparams_audio or self.hparams

        # TODO @ngxson : this is a hack to support both vision and audio encoders
        have_multiple_encoders = self.has_audio_encoder and self.has_vision_encoder
        self.block_count = 128 if have_multiple_encoders else self.find_hparam(self.n_block_keys, True)
        self.tensor_map = gguf.get_tensor_name_map(gguf.MODEL_ARCH.MMPROJ, self.block_count)

        # load preprocessor config
        with open(self.dir_model / "preprocessor_config.json", "r", encoding="utf-8") as f:
            self.preprocessor_config = json.load(f)

    def get_vision_config(self) -> dict[str, Any] | None:
        return self.global_config.get("vision_config")

    def get_audio_config(self) -> dict[str, Any] | None:
        return self.global_config.get("audio_config")

    def set_type(self):
        self.gguf_writer.add_type(gguf.GGUFType.MMPROJ)

    def set_gguf_parameters(self):
        self.gguf_writer.add_file_type(self.ftype)

        if self.has_vision_encoder:
            self.gguf_writer.add_clip_has_vision_encoder(True)
            self.gguf_writer.add_vision_projection_dim(self.n_embd_text)

            # vision config
            self.gguf_writer.add_vision_image_size(self.find_vparam(["image_size"]))
            self.gguf_writer.add_vision_patch_size(self.find_vparam(["patch_size"]))
            self.gguf_writer.add_vision_embedding_length(self.find_vparam(["hidden_size"]))
            self.gguf_writer.add_vision_feed_forward_length(self.find_vparam(["intermediate_size"]))
            self.gguf_writer.add_vision_block_count(self.find_vparam(self.n_block_keys))
            self.gguf_writer.add_vision_head_count(self.find_vparam(["num_attention_heads"]))

            # preprocessor config
            self.gguf_writer.add_vision_image_mean(self.preprocessor_config["image_mean"])
            self.gguf_writer.add_vision_image_std(self.preprocessor_config["image_std"])

        if self.has_audio_encoder:
            self.gguf_writer.add_clip_has_audio_encoder(True)
            self.gguf_writer.add_audio_projection_dim(self.n_embd_text)

            # audio config
            self.gguf_writer.add_audio_embedding_length(self.find_aparam(["hidden_size"]))
            self.gguf_writer.add_audio_feed_forward_length(self.find_aparam(["intermediate_size"]))
            self.gguf_writer.add_audio_block_count(self.find_aparam(self.n_block_keys))
            self.gguf_writer.add_audio_head_count(self.find_aparam(["num_attention_heads"]))

        if not self.has_vision_encoder and not self.has_audio_encoder:
            raise ValueError("MmprojModel must have either vision or audio encoder")

    def write_vocab(self):
        raise ValueError("MmprojModel does not support vocab writing")

    def find_vparam(self, keys: Iterable[str], optional: bool = False) -> Any:
        assert self.hparams_vision is not None
        return self._find_param(self.hparams_vision, keys, optional)

    def find_aparam(self, keys: Iterable[str], optional: bool = False) -> Any:
        assert self.hparams_audio is not None
        return self._find_param(self.hparams_audio, keys, optional)

    def _find_param(self, obj: dict[str, Any], keys: Iterable[str], optional: bool = False) -> Any:
        key = next((k for k in keys if k in obj), None)
        if key is not None:
            return obj[key]
        if optional:
            return None
        raise KeyError(f"could not find any of: {keys}")


@ModelBase.register("GPTNeoXForCausalLM")
class GPTNeoXModel(TextModel):
    model_arch = gguf.MODEL_ARCH.GPTNEOX

    def set_gguf_parameters(self):
        block_count = self.hparams["num_hidden_layers"]

        self.gguf_writer.add_context_length(self.hparams["max_position_embeddings"])
        self.gguf_writer.add_embedding_length(self.hparams["hidden_size"])
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_feed_forward_length(self.hparams["intermediate_size"])
        self.gguf_writer.add_rope_dimension_count(
            int(self.hparams["rotary_pct"] * (self.hparams["hidden_size"] // self.hparams["num_attention_heads"])),
        )
        self.gguf_writer.add_head_count(self.hparams["num_attention_heads"])
        self.gguf_writer.add_parallel_residual(self.hparams.get("use_parallel_residual", True))
        self.gguf_writer.add_layer_norm_eps(self.hparams["layer_norm_eps"])

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        n_head = self.hparams.get("n_head", self.hparams.get("num_attention_heads"))
        n_embed = self.hparams.get("hidden_size", self.hparams.get("n_embed"))

        tensors: list[tuple[str, Tensor]] = []

        if re.match(r"gpt_neox\.layers\.\d+\.attention\.query_key_value\.weight", name):
            # Map bloom-style qkv_linear to gpt-style qkv_linear
            # bloom: https://github.com/huggingface/transformers/blob/main/src/transformers/models/bloom/modeling_bloom.py#L238-L252  # noqa
            # gpt-2: https://github.com/huggingface/transformers/blob/main/src/transformers/models/gpt2/modeling_gpt2.py#L312  # noqa
            qkv_weights = data_torch.reshape((n_head, 3, n_embed // n_head, n_embed))
            data_torch = torch.cat(
                (
                    qkv_weights[:, 0, :, :].reshape((-1, n_embed)),
                    qkv_weights[:, 1, :, :].reshape((-1, n_embed)),
                    qkv_weights[:, 2, :, :].reshape((-1, n_embed)),
                ),
                dim=0,
            )
            logger.info("re-format attention.linear_qkv.weight")
        elif re.match(r"gpt_neox\.layers\.\d+\.attention\.query_key_value\.bias", name):
            qkv_bias = data_torch.reshape((n_head, 3, n_embed // n_head))
            data_torch = torch.cat(
                (
                    qkv_bias[:, 0, :].reshape((n_embed,)),
                    qkv_bias[:, 1, :].reshape((n_embed,)),
                    qkv_bias[:, 2, :].reshape((n_embed,)),
                ),
                dim=0,
            )
            logger.info("re-format attention.linear_qkv.bias")

        tensors.append((self.map_tensor_name(name), data_torch))

        return tensors


@ModelBase.register("BloomForCausalLM", "BloomModel")
class BloomModel(TextModel):
    model_arch = gguf.MODEL_ARCH.BLOOM

    def set_gguf_parameters(self):
        n_embed = self.hparams.get("hidden_size", self.hparams.get("n_embed"))
        n_head = self.hparams.get("n_head", self.hparams.get("num_attention_heads"))
        self.gguf_writer.add_context_length(self.hparams.get("seq_length", n_embed))
        self.gguf_writer.add_embedding_length(n_embed)
        self.gguf_writer.add_feed_forward_length(4 * n_embed)
        self.gguf_writer.add_block_count(self.hparams["n_layer"])
        self.gguf_writer.add_head_count(n_head)
        self.gguf_writer.add_head_count_kv(n_head)
        self.gguf_writer.add_layer_norm_eps(self.hparams["layer_norm_epsilon"])
        self.gguf_writer.add_file_type(self.ftype)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        n_head = self.hparams.get("n_head", self.hparams.get("num_attention_heads"))
        n_embed = self.hparams.get("hidden_size", self.hparams.get("n_embed"))

        name = re.sub(r'transformer\.', '', name)

        tensors: list[tuple[str, Tensor]] = []

        if re.match(r"h\.\d+\.self_attention\.query_key_value\.weight", name):
            # Map bloom-style qkv_linear to gpt-style qkv_linear
            # bloom: https://github.com/huggingface/transformers/blob/main/src/transformers/models/bloom/modeling_bloom.py#L238-L252  # noqa
            # gpt-2: https://github.com/huggingface/transformers/blob/main/src/transformers/models/gpt2/modeling_gpt2.py#L312  # noqa
            qkv_weights = data_torch.reshape((n_head, 3, n_embed // n_head, n_embed))
            data_torch = torch.cat(
                (
                    qkv_weights[:, 0, :, :].reshape((-1, n_embed)),
                    qkv_weights[:, 1, :, :].reshape((-1, n_embed)),
                    qkv_weights[:, 2, :, :].reshape((-1, n_embed)),
                ),
                dim=0,
            )
            logger.info("re-format attention.linear_qkv.weight")
        elif re.match(r"h\.\d+\.self_attention\.query_key_value\.bias", name):
            qkv_bias = data_torch.reshape((n_head, 3, n_embed // n_head))
            data_torch = torch.cat(
                (
                    qkv_bias[:, 0, :].reshape((n_embed,)),
                    qkv_bias[:, 1, :].reshape((n_embed,)),
                    qkv_bias[:, 2, :].reshape((n_embed,)),
                ),
                dim=0,
            )
            logger.info("re-format attention.linear_qkv.bias")

        tensors.append((self.map_tensor_name(name), data_torch))

        return tensors


@ModelBase.register("MPTForCausalLM")
class MPTModel(TextModel):
    model_arch = gguf.MODEL_ARCH.MPT

    def set_vocab(self):
        try:
            self._set_vocab_gpt2()
        except Exception:
            # Fallback for SEA-LION model
            self._set_vocab_sentencepiece()
            self.gguf_writer.add_add_bos_token(False)
            self.gguf_writer.add_pad_token_id(3)
            self.gguf_writer.add_eos_token_id(1)
            self.gguf_writer.add_unk_token_id(0)

    def set_gguf_parameters(self):
        block_count = self.hparams["n_layers"]
        self.gguf_writer.add_context_length(self.hparams["max_seq_len"])
        self.gguf_writer.add_embedding_length(self.hparams["d_model"])
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_feed_forward_length(4 * self.hparams["d_model"])
        self.gguf_writer.add_head_count(self.hparams["n_heads"])
        if kv_n_heads := self.hparams["attn_config"].get("kv_n_heads"):
            self.gguf_writer.add_head_count_kv(kv_n_heads)
        self.gguf_writer.add_layer_norm_eps(1e-5)
        if self.hparams["attn_config"]["clip_qkv"] is not None:
            self.gguf_writer.add_clamp_kqv(self.hparams["attn_config"]["clip_qkv"])
        if self.hparams["attn_config"]["alibi"]:
            self.gguf_writer.add_max_alibi_bias(self.hparams["attn_config"]["alibi_bias_max"])
        else:
            self.gguf_writer.add_max_alibi_bias(0.0)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        if "scales" in name:
            new_name = self.map_tensor_name(name, try_suffixes=(".weight", ".bias", ".scales"))
            new_name = new_name.replace("scales", "act.scales")
        else:
            new_name = self.map_tensor_name(name, try_suffixes=(".weight", ".bias"))

        return [(new_name, data_torch)]


@ModelBase.register("OrionForCausalLM")
class OrionModel(TextModel):
    model_arch = gguf.MODEL_ARCH.ORION

    def set_vocab(self):
        self._set_vocab_sentencepiece()

    def set_gguf_parameters(self):
        block_count = self.hparams["num_hidden_layers"]
        head_count = self.hparams["num_attention_heads"]
        head_count_kv = self.hparams.get("num_key_value_heads", head_count)

        ctx_length = 0
        if "max_sequence_length" in self.hparams:
            ctx_length = self.hparams["max_sequence_length"]
        elif "max_position_embeddings" in self.hparams:
            ctx_length = self.hparams["max_position_embeddings"]
        elif "model_max_length" in self.hparams:
            ctx_length = self.hparams["model_max_length"]
        else:
            raise ValueError("gguf: can not find ctx length parameter.")

        self.gguf_writer.add_file_type(self.ftype)
        self.gguf_writer.add_tensor_data_layout("Meta AI original pth")
        self.gguf_writer.add_context_length(ctx_length)
        self.gguf_writer.add_embedding_length(self.hparams["hidden_size"])
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_feed_forward_length(self.hparams["intermediate_size"])
        self.gguf_writer.add_head_count(head_count)
        self.gguf_writer.add_head_count_kv(head_count_kv)
        # note: config provides rms norm but it is actually layer norm
        # ref:  https://huggingface.co/OrionStarAI/Orion-14B-Chat/blob/276a17221ce42beb45f66fac657a41540e71f4f5/modeling_orion.py#L570-L571
        self.gguf_writer.add_layer_norm_eps(self.hparams["rms_norm_eps"])


@ModelBase.register("BaichuanForCausalLM", "BaiChuanForCausalLM")
class BaichuanModel(TextModel):
    model_arch = gguf.MODEL_ARCH.BAICHUAN

    def set_vocab(self):
        self._set_vocab_sentencepiece()

    def set_gguf_parameters(self):
        block_count = self.hparams["num_hidden_layers"]
        head_count = self.hparams["num_attention_heads"]
        head_count_kv = self.hparams.get("num_key_value_heads", head_count)

        ctx_length = 0
        if "max_sequence_length" in self.hparams:
            ctx_length = self.hparams["max_sequence_length"]
        elif "max_position_embeddings" in self.hparams:
            ctx_length = self.hparams["max_position_embeddings"]
        elif "model_max_length" in self.hparams:
            ctx_length = self.hparams["model_max_length"]
        else:
            raise ValueError("gguf: can not find ctx length parameter.")

        self.gguf_writer.add_tensor_data_layout("Meta AI original pth")
        self.gguf_writer.add_context_length(ctx_length)
        self.gguf_writer.add_embedding_length(self.hparams["hidden_size"])
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_feed_forward_length(self.hparams["intermediate_size"])
        self.gguf_writer.add_rope_dimension_count(self.hparams["hidden_size"] // self.hparams["num_attention_heads"])
        self.gguf_writer.add_head_count(head_count)
        self.gguf_writer.add_head_count_kv(head_count_kv)
        self.gguf_writer.add_layer_norm_rms_eps(self.hparams["rms_norm_eps"])
        self.gguf_writer.add_file_type(self.ftype)

        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "linear" and "factor" in rope_scaling:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.LINEAR)
            self.gguf_writer.add_rope_scaling_factor(rope_scaling["factor"])

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        head_count = self.hparams["num_attention_heads"]
        head_count_kv = self.hparams.get("num_key_value_heads", head_count)

        tensors: list[tuple[str, Tensor]] = []

        if bid is not None and name == f"model.layers.{bid}.self_attn.W_pack.weight":
            logger.info(f"Unpacking and permuting layer {bid}")
            tensors = [
                (self.format_tensor_name(gguf.MODEL_TENSOR.ATTN_Q, bid),
                    self._reverse_hf_permute_part(data_torch, 0, head_count, head_count)),
                (self.format_tensor_name(gguf.MODEL_TENSOR.ATTN_K, bid),
                    self._reverse_hf_permute_part(data_torch, 1, head_count, head_count_kv)),
                (self.format_tensor_name(gguf.MODEL_TENSOR.ATTN_V, bid),
                    self._reverse_hf_part(data_torch, 2)),
            ]
        else:
            tensors = [(self.map_tensor_name(name), data_torch)]

        return tensors

    def _reverse_hf_permute(self, weights: Tensor, n_head: int, n_kv_head: int | None = None) -> Tensor:
        if n_kv_head is not None and n_head != n_kv_head:
            n_head //= n_kv_head

        return (
            weights.reshape(n_head, 2, weights.shape[0] // n_head // 2, *weights.shape[1:])
            .swapaxes(1, 2)
            .reshape(weights.shape)
        )

    def _reverse_hf_permute_part(
        self, weights: Tensor, n_part: int, n_head: int, n_head_kv: int | None = None,
    ) -> Tensor:
        r = weights.shape[0] // 3
        return self._reverse_hf_permute(weights[r * n_part:r * n_part + r, ...], n_head, n_head_kv)

    def _reverse_hf_part(self, weights: Tensor, n_part: int) -> Tensor:
        r = weights.shape[0] // 3
        return weights[r * n_part:r * n_part + r, ...]


@ModelBase.register("XverseForCausalLM")
class XverseModel(TextModel):
    model_arch = gguf.MODEL_ARCH.XVERSE

    def set_vocab(self):
        assert (self.dir_model / "tokenizer.json").is_file()
        dir_model = self.dir_model
        hparams = self.hparams

        tokens: list[bytes] = []
        toktypes: list[int] = []

        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(dir_model)
        vocab_size = hparams.get("vocab_size", len(tokenizer.vocab))
        # Since we are checking the maximum index, we need to ensure it's strictly less than vocab_size,
        # because vocab_size is the count of items, and indexes start at 0.
        max_vocab_index = max(tokenizer.get_vocab().values())
        if max_vocab_index >= vocab_size:
            raise ValueError("Vocabulary size exceeds expected maximum size.")

        reverse_vocab: dict[int, str] = {id_: encoded_tok for encoded_tok, id_ in tokenizer.vocab.items()}
        added_vocab = tokenizer.get_added_vocab()

        for token_id in range(vocab_size):
            token_text = reverse_vocab[token_id].encode('utf-8')
            # replace "\x00" to string with length > 0
            if token_text == b"\x00":
                toktype = gguf.TokenType.BYTE  # special
                token_text = f"<{token_text}>".encode('utf-8')
            elif re.fullmatch(br"<0x[0-9A-Fa-f]{2}>", token_text):
                toktype = gguf.TokenType.BYTE  # special
            elif reverse_vocab[token_id] in added_vocab:
                if tokenizer.added_tokens_decoder[token_id].special:
                    toktype = gguf.TokenType.CONTROL
                else:
                    toktype = gguf.TokenType.USER_DEFINED
            else:
                toktype = gguf.TokenType.NORMAL

            tokens.append(token_text)
            toktypes.append(toktype)

        self.gguf_writer.add_tokenizer_model("llama")
        self.gguf_writer.add_tokenizer_pre("default")
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_types(toktypes)

        special_vocab = gguf.SpecialVocab(dir_model, n_vocab=len(tokens))
        special_vocab.add_to_gguf(self.gguf_writer)

    def set_gguf_parameters(self):
        block_count = self.hparams["num_hidden_layers"]
        head_count = self.hparams["num_attention_heads"]
        head_count_kv = self.hparams.get("num_key_value_heads", head_count)

        ctx_length = 0
        if "max_sequence_length" in self.hparams:
            ctx_length = self.hparams["max_sequence_length"]
        elif "max_position_embeddings" in self.hparams:
            ctx_length = self.hparams["max_position_embeddings"]
        elif "model_max_length" in self.hparams:
            ctx_length = self.hparams["model_max_length"]
        else:
            raise ValueError("gguf: can not find ctx length parameter.")

        self.gguf_writer.add_tensor_data_layout("Meta AI original pth")
        self.gguf_writer.add_context_length(ctx_length)
        self.gguf_writer.add_embedding_length(self.hparams["hidden_size"])
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_feed_forward_length(self.hparams["intermediate_size"])
        self.gguf_writer.add_rope_dimension_count(self.hparams["hidden_size"] // self.hparams["num_attention_heads"])
        self.gguf_writer.add_head_count(head_count)
        self.gguf_writer.add_head_count_kv(head_count_kv)
        self.gguf_writer.add_layer_norm_rms_eps(self.hparams["rms_norm_eps"])
        self.gguf_writer.add_file_type(self.ftype)

        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "linear" and "factor" in rope_scaling:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.LINEAR)
            self.gguf_writer.add_rope_scaling_factor(rope_scaling["factor"])

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        head_count = self.hparams["num_attention_heads"]
        head_count_kv = self.hparams.get("num_key_value_heads", head_count)

        # HF models permute some of the tensors, so we need to undo that
        if name.endswith("q_proj.weight"):
            data_torch = self._reverse_hf_permute(data_torch, head_count, head_count)
        if name.endswith("k_proj.weight"):
            data_torch = self._reverse_hf_permute(data_torch, head_count, head_count_kv)

        return [(self.map_tensor_name(name), data_torch)]

    def _reverse_hf_permute(self, weights: Tensor, n_head: int, n_kv_head: int | None = None) -> Tensor:
        if n_kv_head is not None and n_head != n_kv_head:
            n_head //= n_kv_head

        return (
            weights.reshape(n_head, 2, weights.shape[0] // n_head // 2, *weights.shape[1:])
            .swapaxes(1, 2)
            .reshape(weights.shape)
        )


@ModelBase.register("FalconForCausalLM", "RWForCausalLM")
class FalconModel(TextModel):
    model_arch = gguf.MODEL_ARCH.FALCON

    def set_gguf_parameters(self):
        block_count = self.hparams.get("num_hidden_layers")
        if block_count is None:
            block_count = self.hparams["n_layer"]  # old name

        n_head = self.hparams.get("num_attention_heads")
        if n_head is None:
            n_head = self.hparams["n_head"]  # old name

        n_head_kv = self.hparams.get("num_kv_heads")
        if n_head_kv is None:
            n_head_kv = self.hparams.get("n_head_kv", 1)  # old name

        self.gguf_writer.add_context_length(2048)  # not in config.json
        self.gguf_writer.add_tensor_data_layout("jploski")  # qkv tensor transform
        self.gguf_writer.add_embedding_length(self.hparams["hidden_size"])
        self.gguf_writer.add_feed_forward_length(4 * self.hparams["hidden_size"])
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_head_count(n_head)
        self.gguf_writer.add_head_count_kv(n_head_kv)
        self.gguf_writer.add_layer_norm_eps(self.hparams["layer_norm_epsilon"])
        self.gguf_writer.add_file_type(self.ftype)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        # QKV tensor transform
        # The original query_key_value tensor contains n_head_kv "kv groups",
        # each consisting of n_head/n_head_kv query weights followed by one key
        # and one value weight (shared by all query heads in the kv group).
        # This layout makes it a big pain to work with in GGML.
        # So we rearrange them here,, so that we have n_head query weights
        # followed by n_head_kv key weights followed by n_head_kv value weights,
        # in contiguous fashion.
        # ref: https://github.com/jploski/ggml/blob/falcon40b/examples/falcon/convert-hf-to-ggml.py

        if "query_key_value" in name:
            n_head = self.find_hparam(["num_attention_heads", "n_head"])
            n_head_kv = self.find_hparam(["num_kv_heads", "n_head_kv"], optional=True) or 1
            head_dim = self.hparams["hidden_size"] // n_head

            qkv = data_torch.view(n_head_kv, n_head // n_head_kv + 2, head_dim, head_dim * n_head)
            q = qkv[:, :-2].reshape(n_head * head_dim, head_dim * n_head)
            k = qkv[:, [-2]].reshape(n_head_kv * head_dim, head_dim * n_head)
            v = qkv[:, [-1]].reshape(n_head_kv * head_dim, head_dim * n_head)
            data_torch = torch.cat((q, k, v)).reshape_as(data_torch)

        return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("GPTBigCodeForCausalLM")
class StarCoderModel(TextModel):
    model_arch = gguf.MODEL_ARCH.STARCODER

    def set_gguf_parameters(self):
        block_count = self.hparams["n_layer"]

        self.gguf_writer.add_context_length(self.hparams["n_positions"])
        self.gguf_writer.add_embedding_length(self.hparams["n_embd"])
        self.gguf_writer.add_feed_forward_length(4 * self.hparams["n_embd"])
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_head_count(self.hparams["n_head"])
        self.gguf_writer.add_head_count_kv(1)
        self.gguf_writer.add_layer_norm_eps(self.hparams["layer_norm_epsilon"])
        self.gguf_writer.add_file_type(self.ftype)


@ModelBase.register("GPTRefactForCausalLM")
class RefactModel(TextModel):
    model_arch = gguf.MODEL_ARCH.REFACT

    def set_vocab(self):
        super().set_vocab()

        # TODO: how to determine special FIM tokens automatically?
        special_vocab = gguf.SpecialVocab(self.dir_model, load_merges=False,
                                          special_token_types = ['prefix', 'suffix', 'middle', 'eot'])
        special_vocab._set_special_token("prefix", 1)
        special_vocab._set_special_token("suffix", 3)
        special_vocab._set_special_token("middle", 2)
        special_vocab.chat_template = None  # do not add it twice
        special_vocab.add_to_gguf(self.gguf_writer)

    def set_gguf_parameters(self):
        hidden_dim = self.hparams["n_embd"]
        inner_dim = 4 * hidden_dim
        hidden_dim = int(2 * inner_dim / 3)
        multiple_of = 256
        ff_dim = multiple_of * ((hidden_dim + multiple_of - 1) // multiple_of)

        block_count = self.hparams["n_layer"]

        # refact uses Alibi. So this is from config.json which might be used by training.
        self.gguf_writer.add_context_length(self.hparams["n_positions"])
        self.gguf_writer.add_embedding_length(self.hparams["n_embd"])

        self.gguf_writer.add_feed_forward_length(ff_dim)
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_head_count(self.hparams["n_head"])
        self.gguf_writer.add_head_count_kv(1)
        self.gguf_writer.add_layer_norm_rms_eps(self.hparams["layer_norm_epsilon"])
        self.gguf_writer.add_file_type(self.ftype)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        hidden_dim = self.hparams["n_embd"]
        inner_dim = 4 * hidden_dim
        hidden_dim = int(2 * inner_dim / 3)
        multiple_of = 256
        ff_dim = multiple_of * ((hidden_dim + multiple_of - 1) // multiple_of)
        n_head = self.hparams["n_head"]
        n_head_kv = 1
        head_dim = self.hparams["n_embd"] // n_head

        tensors: list[tuple[str, Tensor]] = []

        if bid is not None:
            if name == f"transformer.h.{bid}.attn.kv.weight":
                tensors.append((self.format_tensor_name(gguf.MODEL_TENSOR.ATTN_K, bid), data_torch[:n_head_kv * head_dim]))
                tensors.append((self.format_tensor_name(gguf.MODEL_TENSOR.ATTN_V, bid), data_torch[n_head_kv * head_dim:]))
            elif name == f"transformer.h.{bid}.attn.q.weight":
                tensors.append((self.format_tensor_name(gguf.MODEL_TENSOR.ATTN_Q, bid), data_torch))
            elif name == f"transformer.h.{bid}.mlp.gate_up_proj.weight":
                tensors.append((self.format_tensor_name(gguf.MODEL_TENSOR.FFN_GATE, bid), data_torch[:ff_dim]))
                tensors.append((self.format_tensor_name(gguf.MODEL_TENSOR.FFN_UP, bid), data_torch[ff_dim:]))

        if len(tensors) == 0:
            tensors.append((self.map_tensor_name(name), data_torch))

        return tensors


@ModelBase.register("StableLmForCausalLM", "StableLMEpochForCausalLM", "LlavaStableLMEpochForCausalLM")
class StableLMModel(TextModel):
    model_arch = gguf.MODEL_ARCH.STABLELM

    def set_vocab(self):
        if (self.dir_model / "tokenizer.json").is_file():
            self._set_vocab_gpt2()
        else:
            # StableLM 2 1.6B used to have a vocab in a similar format to Qwen's vocab
            self._set_vocab_qwen()

    def set_gguf_parameters(self):
        hparams = self.hparams
        block_count = hparams["num_hidden_layers"]

        self.gguf_writer.add_context_length(hparams["max_position_embeddings"])
        self.gguf_writer.add_embedding_length(hparams["hidden_size"])
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_feed_forward_length(hparams["intermediate_size"])
        rotary_factor = self.find_hparam(["partial_rotary_factor", "rope_pct"])
        self.gguf_writer.add_rope_dimension_count(int(rotary_factor * (hparams["hidden_size"] // hparams["num_attention_heads"])))
        self.gguf_writer.add_head_count(hparams["num_attention_heads"])
        self.gguf_writer.add_head_count_kv(hparams["num_key_value_heads"])
        self.gguf_writer.add_parallel_residual(hparams["use_parallel_residual"] if "use_parallel_residual" in hparams else True)
        self.gguf_writer.add_layer_norm_eps(self.find_hparam(["layer_norm_eps", "norm_eps"]))
        self.gguf_writer.add_file_type(self.ftype)

    _q_norms: list[dict[str, Tensor]] | None = None
    _k_norms: list[dict[str, Tensor]] | None = None

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        n_head = self.hparams["num_attention_heads"]
        n_kv_head = self.hparams["num_key_value_heads"]

        if name.find("q_layernorm.norms") != -1:
            assert bid is not None

            if self._q_norms is None:
                self._q_norms = [{} for _ in range(self.block_count)]

            self._q_norms[bid][name] = data_torch

            if len(self._q_norms[bid]) >= n_head:
                return self._stack_qk_norm(bid, n_head, self._q_norms[bid], "q_layernorm")
            else:
                return []

        if name.find("k_layernorm.norms") != -1:
            assert bid is not None

            if self._k_norms is None:
                self._k_norms = [{} for _ in range(self.block_count)]

            self._k_norms[bid][name] = data_torch

            if len(self._k_norms[bid]) >= n_kv_head:
                return self._stack_qk_norm(bid, n_kv_head, self._k_norms[bid], "k_layernorm")
            else:
                return []

        return [(self.map_tensor_name(name), data_torch)]

    def _stack_qk_norm(self, bid: int, n_head: int, norms: dict[str, Tensor], layer_name: str = "q_layernorm"):
        datas: list[Tensor] = []
        # extract the norms in order
        for xid in range(n_head):
            ename = f"model.layers.{bid}.self_attn.{layer_name}.norms.{xid}.weight"
            datas.append(norms[ename])
            del norms[ename]
        data_torch = torch.stack(datas, dim=0)

        merged_name = f"model.layers.{bid}.self_attn.{layer_name}.weight"
        new_name = self.map_tensor_name(merged_name)

        return [(new_name, data_torch)]

    def prepare_tensors(self):
        super().prepare_tensors()

        if self._q_norms is not None or self._k_norms is not None:
            # flatten two `list[dict[str, Tensor]]` into a single `list[str]`
            norms = (
                [k for d in self._q_norms for k in d.keys()] if self._q_norms is not None else []
            ) + (
                [k for d in self._k_norms for k in d.keys()] if self._k_norms is not None else []
            )
            if len(norms) > 0:
                raise ValueError(f"Unprocessed norms: {norms}")


@ModelBase.register(
    "LLaMAForCausalLM",
    "LlamaForCausalLM",
    "MistralForCausalLM",
    "MixtralForCausalLM",
    "VLlama3ForCausalLM",
    "LlavaForConditionalGeneration",
    "LlamaModel")
class LlamaModel(TextModel):
    model_arch = gguf.MODEL_ARCH.LLAMA
    undo_permute = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # fix for SmolVLM2, missing `num_attention_heads` in config.json
        if self.hf_arch == "VLlama3ForCausalLM":
            self.hparams["num_attention_heads"] = self.hparams.get("num_attention_heads", 32)

    def set_vocab(self):
        try:
            self._set_vocab_sentencepiece()
        except FileNotFoundError:
            try:
                self._set_vocab_llama_hf()
            except (FileNotFoundError, TypeError):
                # Llama 3
                self._set_vocab_gpt2()

        # Apply to CodeLlama only (and ignore for Llama 3 with a vocab size of 128256)
        if self.hparams.get("vocab_size", 32000) == 32016:
            special_vocab = gguf.SpecialVocab(
                self.dir_model, load_merges=False,
                special_token_types = ['prefix', 'suffix', 'middle', 'eot']
            )
            special_vocab._set_special_token("prefix", 32007)
            special_vocab._set_special_token("suffix", 32008)
            special_vocab._set_special_token("middle", 32009)
            special_vocab._set_special_token("eot",    32010)
            special_vocab.add_to_gguf(self.gguf_writer)

        tokenizer_config_file = self.dir_model / 'tokenizer_config.json'
        if tokenizer_config_file.is_file():
            with open(tokenizer_config_file, "r", encoding="utf-8") as f:
                tokenizer_config_json = json.load(f)
                if "add_prefix_space" in tokenizer_config_json:
                    self.gguf_writer.add_add_space_prefix(tokenizer_config_json["add_prefix_space"])

        # Apply to granite small models only
        if self.hparams.get("vocab_size", 32000) == 49152:
            self.gguf_writer.add_add_bos_token(False)

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        hparams = self.hparams
        self.gguf_writer.add_vocab_size(hparams["vocab_size"])

        if (rope_dim := hparams.get("head_dim")) is None:
            rope_dim = hparams["hidden_size"] // hparams["num_attention_heads"]
        self.gguf_writer.add_rope_dimension_count(rope_dim)

        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "linear" and "factor" in rope_scaling:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.LINEAR)
            self.gguf_writer.add_rope_scaling_factor(rope_scaling["factor"])

    @staticmethod
    def permute(weights: Tensor, n_head: int, n_head_kv: int | None):
        if n_head_kv is not None and n_head != n_head_kv:
            n_head = n_head_kv
        return (weights.reshape(n_head, 2, weights.shape[0] // n_head // 2, *weights.shape[1:])
                .swapaxes(1, 2)
                .reshape(weights.shape))

    _experts: list[dict[str, Tensor]] | None = None

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        n_head = self.hparams["num_attention_heads"]
        n_kv_head = self.hparams.get("num_key_value_heads")
        is_vision_tensor = "vision_tower" in name \
            or "vision_model" in name \
            or "model.connector" in name \
            or "multi_modal_projector" in name

        if is_vision_tensor:
            return [] # skip vision tensors
        elif self.hf_arch == "LlamaModel":
            name = "model." + name
        elif name.startswith("model.text_model"):
            name = name.replace("text_model.", "") # for SmolVLM
        elif name.startswith("language_model."):
            name = name.replace("language_model.", "") # for the rest

        if self.undo_permute:
            if name.endswith(("q_proj.weight", "q_proj.bias")):
                data_torch = LlamaModel.permute(data_torch, n_head, n_head)
            if name.endswith(("k_proj.weight", "k_proj.bias")):
                data_torch = LlamaModel.permute(data_torch, n_head, n_kv_head)

        # process the experts separately
        if name.find("block_sparse_moe.experts") != -1:
            n_experts = self.hparams["num_local_experts"]

            assert bid is not None

            if self._experts is None:
                self._experts = [{} for _ in range(self.block_count)]

            self._experts[bid][name] = data_torch

            if len(self._experts[bid]) >= n_experts * 3:
                tensors: list[tuple[str, Tensor]] = []

                # merge the experts into a single 3d tensor
                for wid in ["w1", "w2", "w3"]:
                    datas: list[Tensor] = []

                    for xid in range(n_experts):
                        ename = f"model.layers.{bid}.block_sparse_moe.experts.{xid}.{wid}.weight"
                        datas.append(self._experts[bid][ename])
                        del self._experts[bid][ename]

                    data_torch = torch.stack(datas, dim=0)

                    merged_name = f"layers.{bid}.feed_forward.experts.{wid}.weight"

                    new_name = self.map_tensor_name(merged_name)

                    tensors.append((new_name, data_torch))
                return tensors
            else:
                return []

        return [(self.map_tensor_name(name), data_torch)]

    def generate_extra_tensors(self) -> Iterable[tuple[str, Tensor]]:
        if rope_scaling := self.find_hparam(["rope_scaling"], optional=True):
            if rope_scaling.get("rope_type", '').lower() == "llama3":
                base = self.hparams.get("rope_theta", 10000.0)
                if (dim := self.hparams.get("head_dim")) is None:
                    dim = self.hparams["hidden_size"] // self.hparams["num_attention_heads"]
                freqs = 1.0 / (base ** (torch.arange(0, dim, 2, dtype=torch.float32) / dim))

                factor = rope_scaling.get("factor", 8.0)
                low_freq_factor = rope_scaling.get("low_freq_factor", 1.0)
                high_freq_factor = rope_scaling.get("high_freq_factor", 4.0)
                old_context_len = self.hparams.get("original_max_position_embeddings", 8192)

                low_freq_wavelen = old_context_len / low_freq_factor
                high_freq_wavelen = old_context_len / high_freq_factor
                # assert low_freq_wavelen != high_freq_wavelen # Errors for Llama4

                rope_factors = []
                for freq in freqs:
                    wavelen = 2 * math.pi / freq
                    if wavelen < high_freq_wavelen:
                        rope_factors.append(1)
                    elif wavelen > low_freq_wavelen:
                        rope_factors.append(factor)
                    else:
                        smooth = (old_context_len / wavelen - low_freq_factor) / (high_freq_factor - low_freq_factor)
                        rope_factors.append(1 / ((1 - smooth) / factor + smooth))

                yield (self.format_tensor_name(gguf.MODEL_TENSOR.ROPE_FREQS), torch.tensor(rope_factors, dtype=torch.float32))

    def prepare_tensors(self):
        super().prepare_tensors()

        if self._experts is not None:
            # flatten `list[dict[str, Tensor]]` into `list[str]`
            experts = [k for d in self._experts for k in d.keys()]
            if len(experts) > 0:
                raise ValueError(f"Unprocessed experts: {experts}")


@ModelBase.register("ArceeForCausalLM")
class ArceeModel(LlamaModel):
    model_arch = gguf.MODEL_ARCH.ARCEE

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self._try_set_pooling_type()
        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "yarn" and "factor" in rope_scaling:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.YARN)
            self.gguf_writer.add_rope_scaling_factor(rope_scaling["factor"])
            self.gguf_writer.add_rope_scaling_orig_ctx_len(rope_scaling["original_max_position_embeddings"])


@ModelBase.register(
    "LlavaForConditionalGeneration", # pixtral
    "Mistral3ForConditionalGeneration", # mistral small 3.1
)
class LlavaVisionModel(MmprojModel):
    img_break_tok_id = -1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.hparams["model_type"] == "pixtral":
            # layer_norm_eps is not in config.json, it is hard-coded in modeling_pixtral.py
            self.hparams["layer_norm_eps"] = self.hparams.get("layer_norm_eps", 1e-5)
            self.img_break_tok_id = self.get_token_id("[IMG_BREAK]")
            logger.info(f"Image break token id: {self.img_break_tok_id}")
        else:
            raise ValueError(f"Unsupported model type: {self.hparams['model_type']}")

    def get_token_id(self, token: str) -> int:
        tokenizer_config_file = self.dir_model / 'tokenizer_config.json'
        with open(tokenizer_config_file, "r", encoding="utf-8") as f:
            added_tokens_decoder = json.load(f)['added_tokens_decoder']
            for id_, token_data in added_tokens_decoder.items():
                if token_data["content"] == token:
                    return int(id_)
        raise ValueError(f"Token '{token}' not found in tokenizer config.")

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        hparams = self.hparams
        if hparams["model_type"] == "pixtral":
            self.gguf_writer.add_clip_projector_type(gguf.VisionProjectorType.PIXTRAL)
            self.gguf_writer.add_vision_attention_layernorm_eps(hparams["layer_norm_eps"])

            # hidden_act
            if hparams["hidden_act"] == "silu":
                self.gguf_writer.add_vision_use_silu(True)
            elif hparams["hidden_act"] == "gelu":
                self.gguf_writer.add_vision_use_gelu(True)
            else:
                raise ValueError(f"Unsupported hidden_act: {hparams['hidden_act']}")

            # spatial_merge_size
            if "spatial_merge_size" in self.global_config:
                self.gguf_writer.add_vision_spatial_merge_size(self.global_config["spatial_merge_size"])

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused
        n_head = self.hparams["num_attention_heads"]
        n_kv_head = n_head

        if name.startswith("multi_modal_projector.") or name.startswith("vision_tower."):
            # process vision tensors
            if name.endswith(("q_proj.weight", "q_proj.bias")):
                data_torch = LlamaModel.permute(data_torch, n_head, n_head)
            if name.endswith(("k_proj.weight", "k_proj.bias")):
                data_torch = LlamaModel.permute(data_torch, n_head, n_kv_head)
            return [(self.map_tensor_name(name), data_torch)]

        if self.img_break_tok_id > 0 and "embed_tokens.weight" in name:
            logger.info(f"Extracting [IMG_BREAK] token embedding from {name}")
            # for pixtral model, we need to extract the [IMG_BREAK] token embedding
            img_break_embd = data_torch[self.img_break_tok_id]
            name = gguf.TENSOR_NAMES[gguf.MODEL_TENSOR.V_TOK_EMBD_IMG_BREAK]
            return [(self.map_tensor_name(name), img_break_embd)]

        return [] # skip other tensors


@ModelBase.register("Idefics3ForConditionalGeneration", "SmolVLMForConditionalGeneration")
class SmolVLMModel(MmprojModel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.hparams["model_type"] == "smolvlm_vision":
            # fix for SmolVLM2, missing some keys in config.json
            # default values are taken from transformers code
            self.hparams["hidden_size"] = self.hparams.get("hidden_size", 1152)
            self.hparams["num_attention_heads"] = self.hparams.get("num_attention_heads", 16)
            self.hparams["intermediate_size"] = self.hparams.get("intermediate_size", 3072)

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_clip_projector_type(gguf.VisionProjectorType.IDEFICS3)
        self.gguf_writer.add_vision_attention_layernorm_eps(self.hparams.get("layer_norm_eps", 1e-5))
        self.gguf_writer.add_vision_projector_scale_factor(self.global_config.get("scale_factor", 2))
        self.gguf_writer.add_vision_use_gelu(True)

    def tensor_force_quant(self, name, new_name, bid, n_dims):
        del bid, new_name, n_dims  # unused
        if ".embeddings." in name:
            return gguf.GGMLQuantizationType.F32
        return False

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused
        is_vision_tensor = "vision_tower" in name or "vision_model" in name or "model.connector" in name

        if is_vision_tensor:
            return [(self.map_tensor_name(name), data_torch)]

        return [] # skip other tensors


@ModelBase.register("Llama4ForConditionalGeneration")
class Llama4Model(LlamaModel):
    model_arch = gguf.MODEL_ARCH.LLAMA4
    undo_permute = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # IMPORTANT: the normal "intermediate_size" is renamed to "intermediate_size_mlp", we need to undo this
        self.hparams["intermediate_size_moe"] = self.hparams["intermediate_size"]
        self.hparams["intermediate_size"] = self.hparams["intermediate_size_mlp"]

    def set_vocab(self):
        self._set_vocab_gpt2()

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_interleave_moe_layer_step(self.hparams["interleave_moe_layer_step"])
        self.gguf_writer.add_expert_feed_forward_length(self.hparams["intermediate_size_moe"])

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None):
        if name.startswith("language_model."):
            name = name.replace("language_model.", "")

        # split the gate_up into gate and up
        if "gate_up_proj" in name:
            name_up = name.replace("gate_up_proj", "up_proj.weight")
            name_gate = name.replace("gate_up_proj", "gate_proj.weight")
            dim_half = data_torch.shape[-1] // 2
            gate_proj_weight, up_proj_weight = data_torch.transpose(-1, -2).split(dim_half, dim=-2)
            return [
                (self.map_tensor_name(name_gate), gate_proj_weight),
                (self.map_tensor_name(name_up), up_proj_weight)
            ]

        if name.endswith("down_proj"):
            name += ".weight"
            data_torch = data_torch.transpose(-1, -2)

        if "multi_modal_projector" in name or "vision_model" in name:
            return []
        return super().modify_tensors(data_torch, name, bid)


@ModelBase.register("Llama4ForConditionalGeneration")
class Llama4VisionModel(MmprojModel):
    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_clip_projector_type(gguf.VisionProjectorType.LLAMA4)
        self.gguf_writer.add_vision_attention_layernorm_eps(self.hparams["norm_eps"])
        self.gguf_writer.add_vision_projector_scale_factor(int(1.0 / self.hparams["pixel_shuffle_ratio"]))
        assert self.hparams["hidden_act"] == "gelu"
        self.gguf_writer.add_vision_use_gelu(True)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid # unused
        if "multi_modal_projector" in name or "vision_model" in name:
            # process vision tensors
            if "positional_embedding_vlm" in name and ".weight" not in name:
                name += ".weight"
            if "multi_modal_projector.linear_1" in name:
                # despite the name with number postfix, this is a single fully connected layer
                return [(gguf.TENSOR_NAMES[gguf.MODEL_TENSOR.V_MMPROJ_FC] + '.weight', data_torch)]
            return [(self.map_tensor_name(name), data_torch)]
        return []


@ModelBase.register("Mistral3ForConditionalGeneration")
class Mistral3Model(LlamaModel):
    model_arch = gguf.MODEL_ARCH.LLAMA

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None):
        name = name.replace("language_model.", "")
        if "multi_modal_projector" in name or "vision_tower" in name:
            return []
        return super().modify_tensors(data_torch, name, bid)


@ModelBase.register("DeciLMForCausalLM")
class DeciModel(TextModel):
    model_arch = gguf.MODEL_ARCH.DECI

    @staticmethod
    def _ffn_mult_to_intermediate_size(ffn_mult: float, n_embd: int) -> int:
        # DeciLM-specific code
        intermediate_size = int(2 * ffn_mult * n_embd / 3)
        return DeciModel._find_multiple(intermediate_size, 256)

    @staticmethod
    def _find_multiple(n: int, k: int) -> int:
        # DeciLM-specific code
        if n % k == 0:
            return n
        return n + k - (n % k)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if "block_configs" in self.hparams: # Llama-3_1-Nemotron-51B
            _block_configs: list[dict[str,Any]] = self.hparams["block_configs"]
            assert self.block_count == len(_block_configs)
            self._num_kv_heads = list()
            self._num_heads = list()
            _ffn_multipliers = list()
            # ***linear attention layer***
            # if n_heads_in_group is None and replace_with_linear is True
            # then _num_kv_heads[il] is 0 and _num_heads[il] is num_attention_heads
            # ***attention-free layer***
            # if n_heads_in_group is None and replace_with_linear is False
            # then _num_kv_heads[il] is 0 and _num_heads[il] is 0
            # ***normal attention-layer***
            # if n_heads_in_group is not None, then
            # _num_kv_heads[il] is num_attention_head // n_heads_in_group and
            # _num_heads[il] is num_attention_head
            # ***dummy layer*** for nemotron 253B
            # if n_heads_in_group is None and ffn_mult is None
            # then _num_kv_heads[il] is 0 and _num_heads[il] is 0 and _ffn_dims is 0
            for il in range(len(_block_configs)):
                if _block_configs[il]["attention"]["n_heads_in_group"] is None:
                    if _block_configs[il]["attention"]["replace_with_linear"] is True:
                        self._num_kv_heads.append(0)
                        self._num_heads.append(self.hparams["num_attention_heads"])
                    else:
                        self._num_kv_heads.append(0)
                        self._num_heads.append(0)
                else:
                    self._num_kv_heads.append(self.hparams["num_attention_heads"] // _block_configs[il]["attention"]["n_heads_in_group"])
                    self._num_heads.append(self.hparams["num_attention_heads"])
                if _block_configs[il]["ffn"]["ffn_mult"] is None: # dummy layer
                    _ffn_multipliers.append(0.0)
                else:
                    _ffn_multipliers.append(_block_configs[il]["ffn"]["ffn_mult"])
            assert self.block_count == len(self._num_kv_heads)
            assert self.block_count == len(self._num_heads)
            assert self.block_count == len(_ffn_multipliers)
            assert isinstance(self._num_kv_heads, list) and isinstance(self._num_kv_heads[0], int)
            assert isinstance(self._num_heads, list) and isinstance(self._num_heads[0], int)
            assert isinstance(_ffn_multipliers, list) and isinstance(_ffn_multipliers[0], float)
            self._ffn_dims: list[int] = [
                DeciModel._ffn_mult_to_intermediate_size(multiplier, self.hparams["hidden_size"])
                for multiplier in _ffn_multipliers
            ]

    def set_vocab(self):
        # Please change tokenizer_config.json of Llama-3_1-Nemotron-51B's
        # eos_token from '|eot_id|' to '|end_of_text|'
        if self.hparams.get("vocab_size", 128256) == 128256:
            tokens, toktypes, tokpre = self.get_vocab_base()
            self.gguf_writer.add_tokenizer_model("gpt2")
            self.gguf_writer.add_tokenizer_pre(tokpre)
            self.gguf_writer.add_token_list(tokens)
            self.gguf_writer.add_token_types(toktypes)

            special_vocab = gguf.SpecialVocab(self.dir_model, load_merges=True)
            special_vocab.add_to_gguf(self.gguf_writer)
        else:
            # DeciLM-7B
            self._set_vocab_llama_hf()

    def set_gguf_parameters(self):
        if "block_configs" in self.hparams: # Llama-3_1-Nemotron-51B
            assert self.block_count == len(self._num_kv_heads)
            assert self.block_count == len(self._num_heads)
            assert self.block_count == len(self._ffn_dims)
            if (rope_theta := self.hparams.get("rope_theta")) is not None:
                self.gguf_writer.add_rope_freq_base(rope_theta)
            self.gguf_writer.add_head_count_kv(self._num_kv_heads)
            self.gguf_writer.add_head_count(self._num_heads)
            self.gguf_writer.add_feed_forward_length(self._ffn_dims)
            self.gguf_writer.add_block_count(self.block_count)
            self.gguf_writer.add_context_length(self.hparams["max_position_embeddings"])
            self.gguf_writer.add_embedding_length(self.hparams["hidden_size"])
            self.gguf_writer.add_layer_norm_rms_eps(self.hparams["rms_norm_eps"])
            self.gguf_writer.add_key_length(self.hparams["hidden_size"] // self.hparams["num_attention_heads"])
            self.gguf_writer.add_value_length(self.hparams["hidden_size"] // self.hparams["num_attention_heads"])
            self.gguf_writer.add_file_type(self.ftype)
        else: # DeciLM-7B
            super().set_gguf_parameters()
            if "num_key_value_heads_per_layer" in self.hparams: # DeciLM-7B
                self._num_kv_heads: list[int] = self.hparams["num_key_value_heads_per_layer"]
                assert self.block_count == len(self._num_kv_heads)
                self.gguf_writer.add_head_count_kv(self._num_kv_heads)
        hparams = self.hparams
        self.gguf_writer.add_vocab_size(hparams["vocab_size"])

        if (rope_dim := hparams.get("head_dim")) is None:
            rope_dim = hparams["hidden_size"] // hparams["num_attention_heads"]
        self.gguf_writer.add_rope_dimension_count(rope_dim)

        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "linear" and "factor" in rope_scaling:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.LINEAR)
            self.gguf_writer.add_rope_scaling_factor(rope_scaling["factor"])

    @staticmethod
    def permute(weights: Tensor, n_head: int, n_head_kv: int | None):
        if n_head_kv is not None and n_head != n_head_kv:
            n_head = n_head_kv
        return (weights.reshape(n_head, 2, weights.shape[0] // n_head // 2, *weights.shape[1:])
                .swapaxes(1, 2)
                .reshape(weights.shape))

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        n_head = self.hparams["num_attention_heads"]
        if bid is not None:
            if "num_key_value_heads_per_layer" in self.hparams:
                n_kv_head = self.hparams["num_key_value_heads_per_layer"][bid]
            elif "block_configs" in self.hparams:
                n_kv_head = self._num_kv_heads[bid]
                n_head = self._num_heads[bid]
            else:
                n_kv_head = self.hparams.get("num_key_value_heads")
        else:
            n_kv_head = self.hparams.get("num_key_value_heads")

        if name.endswith(("q_proj.weight", "q_proj.bias")):
            data_torch = DeciModel.permute(data_torch, n_head, n_head)
        if name.endswith(("k_proj.weight", "k_proj.bias")):
            data_torch = DeciModel.permute(data_torch, n_head, n_kv_head)
        return [(self.map_tensor_name(name), data_torch)]

    def generate_extra_tensors(self) -> Iterable[tuple[str, Tensor]]:
        if rope_scaling := self.find_hparam(["rope_scaling"], optional=True):
            if rope_scaling.get("rope_type", '').lower() == "llama3":
                base = self.hparams.get("rope_theta", 10000.0)
                if (dim := self.hparams.get("head_dim")) is None:
                    dim = self.hparams["hidden_size"] // self.hparams["num_attention_heads"]
                freqs = 1.0 / (base ** (torch.arange(0, dim, 2, dtype=torch.float32) / dim))

                factor = rope_scaling.get("factor", 8.0)
                low_freq_factor = rope_scaling.get("low_freq_factor", 1.0)
                high_freq_factor = rope_scaling.get("high_freq_factor", 4.0)
                old_context_len = self.hparams.get("original_max_position_embeddings", 8192)

                low_freq_wavelen = old_context_len / low_freq_factor
                high_freq_wavelen = old_context_len / high_freq_factor
                assert low_freq_wavelen != high_freq_wavelen

                rope_factors = []
                for freq in freqs:
                    wavelen = 2 * math.pi / freq
                    if wavelen < high_freq_wavelen:
                        rope_factors.append(1)
                    elif wavelen > low_freq_wavelen:
                        rope_factors.append(factor)
                    else:
                        smooth = (old_context_len / wavelen - low_freq_factor) / (high_freq_factor - low_freq_factor)
                        rope_factors.append(1 / ((1 - smooth) / factor + smooth))

                yield (self.format_tensor_name(gguf.MODEL_TENSOR.ROPE_FREQS), torch.tensor(rope_factors, dtype=torch.float32))

    def prepare_tensors(self):
        super().prepare_tensors()


@ModelBase.register("BitnetForCausalLM")
class BitnetModel(TextModel):
    model_arch = gguf.MODEL_ARCH.BITNET

    def set_vocab(self):
        self._set_vocab_sentencepiece()

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.LINEAR)
        self.gguf_writer.add_rope_scaling_factor(1.0)

    def weight_quant(self, weight: Tensor) -> Tensor:
        dtype = weight.dtype
        weight = weight.float()
        scale = weight.abs().mean().clamp(min=1e-5)
        iscale = 1 / scale
        # TODO: multiply by the scale directly instead of inverting it twice
        # (this is also unnecessarily doubly inverted upstream)
        # ref: https://huggingface.co/1bitLLM/bitnet_b1_58-3B/blob/af89e318d78a70802061246bf037199d2fb97020/utils_quant.py#L10
        result = (weight * iscale).round().clamp(-1, 1) / iscale
        return result.type(dtype)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        new_name = self.map_tensor_name(name)

        if any(self.match_model_tensor_name(new_name, key, bid) for key in [
            gguf.MODEL_TENSOR.ATTN_Q,
            gguf.MODEL_TENSOR.ATTN_K,
            gguf.MODEL_TENSOR.ATTN_V,
            gguf.MODEL_TENSOR.ATTN_OUT,
            gguf.MODEL_TENSOR.FFN_UP,
            gguf.MODEL_TENSOR.FFN_DOWN,
            gguf.MODEL_TENSOR.FFN_GATE,
        ]):
            # transform weight into 1/0/-1 (in fp32)
            data_torch = self.weight_quant(data_torch)

        yield (new_name, data_torch)


@ModelBase.register("GrokForCausalLM")
class GrokModel(TextModel):
    model_arch = gguf.MODEL_ARCH.GROK

    def set_vocab(self):
        self._set_vocab_sentencepiece()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def set_gguf_parameters(self):
        super().set_gguf_parameters()

    _experts: list[dict[str, Tensor]] | None = None

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        # process the experts separately
        if name.find(".moe.") != -1:
            n_experts = self.hparams["num_local_experts"]

            assert bid is not None

            if self._experts is None:
                self._experts = [{} for _ in range(self.block_count)]

            self._experts[bid][name] = data_torch

            if len(self._experts[bid]) >= n_experts * 3:
                tensors: list[tuple[str, Tensor]] = []

                # merge the experts into a single 3d tensor
                for wid in ["linear", "linear_1", "linear_v"]:
                    datas: list[Tensor] = []

                    for xid in range(n_experts):
                        ename = f"transformer.decoder_layer.{bid}.moe.{xid}.{wid}.weight"
                        datas.append(self._experts[bid][ename])
                        del self._experts[bid][ename]

                    data_torch = torch.stack(datas, dim=0)

                    merged_name = f"transformer.decoder_layer.{bid}.moe.{wid}.weight"

                    new_name = self.map_tensor_name(merged_name)

                    tensors.append((new_name, data_torch))
                return tensors
            else:
                return []

        return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("DbrxForCausalLM")
class DbrxModel(TextModel):
    model_arch = gguf.MODEL_ARCH.DBRX

    def set_gguf_parameters(self):
        ffn_config = self.hparams["ffn_config"]
        attn_config = self.hparams["attn_config"]
        self.gguf_writer.add_block_count(self.hparams["n_layers"])

        self.gguf_writer.add_context_length(self.hparams["max_seq_len"])
        self.gguf_writer.add_embedding_length(self.hparams["d_model"])
        self.gguf_writer.add_feed_forward_length(ffn_config["ffn_hidden_size"])

        self.gguf_writer.add_head_count(self.hparams["n_heads"])
        self.gguf_writer.add_head_count_kv(attn_config["kv_n_heads"])

        self.gguf_writer.add_rope_freq_base(attn_config["rope_theta"])

        self.gguf_writer.add_clamp_kqv(attn_config["clip_qkv"])

        self.gguf_writer.add_expert_count(ffn_config["moe_num_experts"])
        self.gguf_writer.add_expert_used_count(ffn_config["moe_top_k"])

        self.gguf_writer.add_layer_norm_eps(1e-5)

        self.gguf_writer.add_file_type(self.ftype)
        logger.info(f"gguf: file type = {self.ftype}")

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        n_expert = self.hparams["ffn_config"]["moe_num_experts"]
        n_ff = self.hparams["ffn_config"]["ffn_hidden_size"]
        n_embd = self.hparams["d_model"]

        # Specific behavior for experts tensors: suffix .weight, view as 3D and transpose
        # original implementation expects (n_expert, n_ff, n_embd) for all experts weights
        # But llama.cpp moe graph works differently
        # AND the dimensions in ggml are typically in the reverse order of the pytorch dimensions
        # so (n_expert, n_ff, n_embd) in pytorch is {n_embd, n_ff, n_expert} in ggml_tensor
        exp_tensor_names = {"ffn.experts.mlp.w1": None,       # LLM_TENSOR_FFN_GATE_EXPS ggml_tensor->ne{n_embd, n_ff,   n_expert}
                            "ffn.experts.mlp.w2": (0, 2, 1),  # LLM_TENSOR_FFN_DOWN_EXPS ggml_tensor->ne{n_ff,   n_embd, n_expert}
                            "ffn.experts.mlp.v1": None}       # LLM_TENSOR_FFN_UP_EXPS   ggml_tensor->ne{n_embd, n_ff,   n_expert}
        experts = False

        for exp_tensor_name in exp_tensor_names.keys():
            if name.find(exp_tensor_name) != -1 and name.find(".weight") == -1:
                experts = True
                data_torch = data_torch.view(n_expert, n_ff, n_embd)
                if (permute_tensor := exp_tensor_names[exp_tensor_name]) is not None:
                    data_torch = data_torch.permute(*permute_tensor)
                break

        # map tensor names
        # In MoE models the ffn tensors are typically most of the model weights,
        # and need to be quantizable. Quantize expects tensor names to be suffixed by .weight.
        # Every other model has the weight names ending in .weight,
        # let's assume that is the convention which is not the case for dbrx:
        # https://huggingface.co/databricks/dbrx-instruct/blob/main/model.safetensors.index.json#L15
        new_name = self.map_tensor_name(name if not experts else name + ".weight", try_suffixes=(".weight",))

        return [(new_name, data_torch)]

    def tensor_force_quant(self, name: str, new_name: str, bid: int | None, n_dims: int) -> gguf.GGMLQuantizationType | bool:
        del name, new_name, bid  # unused

        return n_dims > 1


@ModelBase.register("MiniCPMForCausalLM")
class MiniCPMModel(TextModel):
    model_arch = gguf.MODEL_ARCH.MINICPM

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        embedding_scale = float(self.hparams["scale_emb"])
        self.gguf_writer.add_embedding_scale(embedding_scale)
        logger.info(f"gguf: (minicpm) embedding_scale = {embedding_scale}")
        residual_scale = self.hparams["scale_depth"] / self.hparams["num_hidden_layers"] ** 0.5
        self.gguf_writer.add_residual_scale(residual_scale)
        logger.info(f"gguf: (minicpm) residual_scale = {residual_scale}")
        logit_scale = self.hparams["hidden_size"] / self.hparams["dim_model_base"]
        self.gguf_writer.add_logit_scale(logit_scale)
        logger.info(f"gguf: (minicpm) logit_scale = {logit_scale}")
        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "longrope":
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.LONGROPE)
            logger.info(f"gguf: (minicpm) rope_scaling_type = {gguf.RopeScalingType.LONGROPE}")

    def generate_extra_tensors(self) -> Iterable[tuple[str, Tensor]]:
        rope_dims = self.hparams["hidden_size"] // self.hparams["num_attention_heads"]

        rope_scaling = self.find_hparam(['rope_scaling'], True)
        if rope_scaling is not None:
            long_factors = rope_scaling.get('long_factor', None)
            short_factors = rope_scaling.get('short_factor', None)

            if long_factors is None or short_factors is None:
                raise KeyError('Missing the required key rope_scaling.long_factor or rope_scaling_short_factor')

            if len(long_factors) != len(short_factors) or len(long_factors) != rope_dims / 2:
                raise ValueError(f'The length of rope long and short factors must be {rope_dims / 2}')

            yield (self.format_tensor_name(gguf.MODEL_TENSOR.ROPE_FACTORS_LONG), torch.tensor(long_factors, dtype=torch.float32))
            yield (self.format_tensor_name(gguf.MODEL_TENSOR.ROPE_FACTORS_SHORT), torch.tensor(short_factors, dtype=torch.float32))

    def set_vocab(self):
        self._set_vocab_sentencepiece()

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        n_head = self.hparams["num_attention_heads"]
        n_kv_head = self.hparams.get("num_key_value_heads")

        # HF models permute some of the tensors, so we need to undo that
        if name.endswith(("q_proj.weight")):
            data_torch = LlamaModel.permute(data_torch, n_head, n_head)
        if name.endswith(("k_proj.weight")):
            data_torch = LlamaModel.permute(data_torch, n_head, n_kv_head)

        return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("MiniCPM3ForCausalLM")
class MiniCPM3Model(TextModel):
    model_arch = gguf.MODEL_ARCH.MINICPM3

    def set_gguf_parameters(self):
        hparams = self.hparams

        self.gguf_writer.add_file_type(self.ftype)
        self.gguf_writer.add_context_length(hparams["max_position_embeddings"])
        self.gguf_writer.add_embedding_length(hparams["hidden_size"])
        self.gguf_writer.add_block_count(self.block_count)
        self.gguf_writer.add_feed_forward_length(hparams["intermediate_size"])
        self.gguf_writer.add_head_count(hparams["num_attention_heads"])
        self.gguf_writer.add_head_count_kv(hparams["num_key_value_heads"])
        self.gguf_writer.add_layer_norm_rms_eps(hparams["rms_norm_eps"])
        self.gguf_writer.add_vocab_size(hparams["vocab_size"])
        if "q_lora_rank" in hparams and hparams["q_lora_rank"] is not None:
            self.gguf_writer.add_q_lora_rank(hparams["q_lora_rank"])
        self.gguf_writer.add_kv_lora_rank(hparams["kv_lora_rank"])
        self.gguf_writer.add_key_length(hparams["qk_nope_head_dim"] + hparams["qk_rope_head_dim"])
        self.gguf_writer.add_rope_dimension_count(hparams["qk_rope_head_dim"])

    def generate_extra_tensors(self) -> Iterable[tuple[str, Tensor]]:
        rope_scaling = self.find_hparam(['rope_scaling'], True)
        if rope_scaling is not None:
            rope_dims = self.hparams["qk_rope_head_dim"]

            long_factors = rope_scaling.get('long_factor', None)
            short_factors = rope_scaling.get('short_factor', None)

            if long_factors is None or short_factors is None:
                raise KeyError('Missing the required key rope_scaling.long_factor or rope_scaling_short_factor')

            if len(long_factors) != len(short_factors) or len(long_factors) != rope_dims / 2:
                raise ValueError(f'The length of rope long and short factors must be {rope_dims / 2}')

            yield (self.format_tensor_name(gguf.MODEL_TENSOR.ROPE_FACTORS_LONG), torch.tensor(long_factors, dtype=torch.float32))
            yield (self.format_tensor_name(gguf.MODEL_TENSOR.ROPE_FACTORS_SHORT), torch.tensor(short_factors, dtype=torch.float32))

    def set_vocab(self):
        self._set_vocab_sentencepiece()

    def _reverse_hf_permute(self, weights: Tensor, n_head: int, n_kv_head: int | None = None) -> Tensor:
        if n_kv_head is not None and n_head != n_kv_head:
            n_head //= n_kv_head

        return (
            weights.reshape(n_head, 2, weights.shape[0] // n_head // 2, *weights.shape[1:])
            .swapaxes(1, 2)
            .reshape(weights.shape)
        )


@ModelBase.register("QWenLMHeadModel")
class QwenModel(TextModel):
    model_arch = gguf.MODEL_ARCH.QWEN

    @staticmethod
    def token_bytes_to_string(b):
        from transformers.models.gpt2.tokenization_gpt2 import bytes_to_unicode
        byte_encoder = bytes_to_unicode()
        return ''.join([byte_encoder[ord(char)] for char in b.decode('latin-1')])

    @staticmethod
    def bpe(mergeable_ranks: dict[bytes, int], token: bytes, max_rank: int | None = None) -> list[bytes]:
        parts = [bytes([b]) for b in token]
        while True:
            min_idx = None
            min_rank = None
            for i, pair in enumerate(zip(parts[:-1], parts[1:])):
                rank = mergeable_ranks.get(pair[0] + pair[1])
                if rank is not None and (min_rank is None or rank < min_rank):
                    min_idx = i
                    min_rank = rank
            if min_rank is None or (max_rank is not None and min_rank >= max_rank):
                break
            assert min_idx is not None
            parts = parts[:min_idx] + [parts[min_idx] + parts[min_idx + 1]] + parts[min_idx + 2:]
        return parts

    def set_vocab(self):
        self._set_vocab_qwen()

    def set_gguf_parameters(self):
        self.gguf_writer.add_context_length(self.hparams["max_position_embeddings"])
        self.gguf_writer.add_block_count(self.hparams["num_hidden_layers"])
        self.gguf_writer.add_embedding_length(self.hparams["hidden_size"])
        self.gguf_writer.add_feed_forward_length(self.hparams["intermediate_size"])
        self.gguf_writer.add_rope_freq_base(self.hparams["rotary_emb_base"])
        self.gguf_writer.add_rope_dimension_count(self.hparams["hidden_size"] // self.hparams["num_attention_heads"])
        self.gguf_writer.add_head_count(self.hparams["num_attention_heads"])
        self.gguf_writer.add_layer_norm_rms_eps(self.hparams["layer_norm_epsilon"])
        self.gguf_writer.add_file_type(self.ftype)


@ModelBase.register("Qwen2Model", "Qwen2ForCausalLM", "Qwen2AudioForConditionalGeneration")
class Qwen2Model(TextModel):
    model_arch = gguf.MODEL_ARCH.QWEN2

    def set_vocab(self):
        try:
            self._set_vocab_sentencepiece()
        except FileNotFoundError:
            self._set_vocab_gpt2()

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self._try_set_pooling_type()
        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "yarn" and "factor" in rope_scaling:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.YARN)
            self.gguf_writer.add_rope_scaling_factor(rope_scaling["factor"])
            self.gguf_writer.add_rope_scaling_orig_ctx_len(rope_scaling["original_max_position_embeddings"])

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        if self.hf_arch == "Qwen2Model":
            name = f"model.{name}"  # map to Qwen2ForCausalLM tensors
        if "language_model." in name:
            name = name.replace("language_model.", "") # for InternVL
        if name.startswith("mlp") or name.startswith("multi_modal_projector") \
                or name.startswith("vision_model") or name.startswith("audio_tower"):
            # skip vision and audio tensors
            return []
        yield from super().modify_tensors(data_torch, name, bid)


@ModelBase.register("DreamModel")
class DreamModel(TextModel):
    model_arch = gguf.MODEL_ARCH.DREAM

    def get_vocab_base(self) -> tuple[list[str], list[int], str]:
        tokens: list[str] = []
        toktypes: list[int] = []

        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(self.dir_model, trust_remote_code=True)

        vocab_dict = tokenizer.get_vocab()
        vocab_size = self.hparams.get("vocab_size", len(vocab_dict))
        assert max(vocab_dict.values()) < vocab_size

        tokpre = self.get_vocab_base_pre(tokenizer)

        reverse_vocab = {id_: encoded_tok for encoded_tok, id_ in vocab_dict.items()}
        added_vocab = tokenizer.get_added_vocab()

        for i in range(vocab_size):
            if i not in reverse_vocab:
                tokens.append(f"[PAD{i}]")
                toktypes.append(gguf.TokenType.UNUSED)
            elif reverse_vocab[i] in added_vocab:
                tokens.append(reverse_vocab[i])
                # Check if it's a special token - treat special tokens as CONTROL tokens
                if hasattr(tokenizer, 'added_tokens_decoder') and i in tokenizer.added_tokens_decoder:
                    if tokenizer.added_tokens_decoder[i].special:
                        toktypes.append(gguf.TokenType.CONTROL)
                    else:
                        toktypes.append(gguf.TokenType.USER_DEFINED)
                else:
                    # Fallback: treat all added vocab as control tokens for special tokens like <|im_start|>
                    toktypes.append(gguf.TokenType.CONTROL)
            else:
                tokens.append(reverse_vocab[i])
                toktypes.append(gguf.TokenType.NORMAL)

        return tokens, toktypes, tokpre

    def set_vocab(self):
        try:
            self._set_vocab_sentencepiece()
        except FileNotFoundError:
            self._set_vocab_gpt2()

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self._try_set_pooling_type()

        # Dream models use non-causal attention for diffusion
        self.gguf_writer.add_causal_attention(False)
        # Handle RoPE scaling similar to Qwen2
        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "yarn" and "factor" in rope_scaling:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.YARN)
            self.gguf_writer.add_rope_scaling_factor(rope_scaling["factor"])
            self.gguf_writer.add_rope_scaling_orig_ctx_len(rope_scaling["original_max_position_embeddings"])

        # Add Dream-specific parameters
        mask_token_id = self.hparams.get("mask_token_id")
        if mask_token_id is not None:
            self.gguf_writer.add_mask_token_id(mask_token_id)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        # Dream model tensors should be mapped directly since it's the base model
        yield from super().modify_tensors(data_torch, name, bid)


@ModelBase.register("Ernie4_5_ForCausalLM")
class Ernie4_5Model(TextModel):
    model_arch = gguf.MODEL_ARCH.ERNIE4_5

    def set_vocab(self):
        self._set_vocab_sentencepiece()

    def set_gguf_parameters(self):
        super().set_gguf_parameters()

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        num_heads = self.hparams["num_attention_heads"]
        num_kv_heads = self.hparams["num_key_value_heads"]
        if (head_dim := self.hparams.get("head_dim")) is None:
            head_dim = self.hparams["hidden_size"] // num_heads

        if "ernie." in name:
            name = name.replace("ernie.", "model.")
        # split the qkv weights
        # qkv_proj shape: [(num_heads + 2 * num_kv_heads) * head_dim, hidden_size]
        if "qkv_proj" in name:
            name_q = name.replace("qkv_proj.weight", "q_proj.weight")
            name_k = name.replace("qkv_proj.weight", "k_proj.weight")
            name_v = name.replace("qkv_proj.weight", "v_proj.weight")
            total_q_dim = num_heads * head_dim
            total_k_dim = num_kv_heads * head_dim
            total_v_dim = num_kv_heads * head_dim
            q_proj_weight, k_proj_weight, v_proj_weight = data_torch.split([total_q_dim, total_k_dim, total_v_dim], dim=0)
            return [
                (self.map_tensor_name(name_q), q_proj_weight),
                (self.map_tensor_name(name_k), k_proj_weight),
                (self.map_tensor_name(name_v), v_proj_weight)
            ]
        # split the up_gate_proj into gate and up
        # up_gate_proj shape: [2 * intermediate_size, hidden_size]
        if "up_gate_proj" in name:
            name_up = name.replace("up_gate_proj.weight", "up_proj.weight")
            name_gate = name.replace("up_gate_proj.weight", "gate_proj.weight")
            dim_half = data_torch.shape[0] // 2
            gate_proj_weight, up_proj_weight = data_torch.split(dim_half, dim=0)
            return [
                (self.map_tensor_name(name_gate), gate_proj_weight),
                (self.map_tensor_name(name_up), up_proj_weight)
            ]
        return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("Ernie4_5_MoeForCausalLM")
class Ernie4_5MoeModel(Ernie4_5Model):
    model_arch = gguf.MODEL_ARCH.ERNIE4_5_MOE
    _experts: list[dict[str, Tensor]] | None = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._experts = [{} for _ in range(self.block_count)]

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_expert_count(self.hparams["moe_num_experts"])
        self.gguf_writer.add_expert_used_count(self.hparams["moe_k"])
        self.gguf_writer.add_interleave_moe_layer_step(self.hparams["moe_layer_interval"])
        self.gguf_writer.add_leading_dense_block_count(self.hparams["moe_layer_start_index"])
        if (moe_intermediate_size := self.hparams.get("moe_intermediate_size")) is not None:
            self.gguf_writer.add_expert_feed_forward_length(moe_intermediate_size)
        if (shared_expert_count := self.hparams.get('moe_num_shared_experts')) is not None:
            self.gguf_writer.add_expert_shared_count(shared_expert_count)
            if shared_expert_count > 0 and (shared_expert_intermediate_size := self.hparams.get('intermediate_size')) is not None and (num_key_value_heads := self.hparams.get('num_key_value_heads')) is not None:
                self.gguf_writer.add_expert_shared_feed_forward_length(shared_expert_intermediate_size // num_key_value_heads)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        # Modify correction bias name as in DeepseekV2
        if name.endswith("e_score_correction_bias"):
            name = name.replace("e_score_correction_bias", "e_score_correction.bias")

        # skip Multi-Token Prediction (MTP) layers (again, same as DeepseekV2)
        match = re.match(r"model.mtp_block.(\d+)", name)
        if match:
            return []

        # skip all other MTP tensors for now
        match = re.match(r"model.mtp_emb_norm.(\d+)", name)
        if match:
            return []

        match = re.match(r"model.mtp_hidden_norm.(\d+)", name)
        if match:
            return []

        match = re.match(r"model.mtp_linear_proj.(\d+)", name)
        if match:
            return []

        # process the experts separately
        if name.find("mlp.experts") != -1:
            n_experts = self.hparams["moe_num_experts"]
            assert bid is not None

            if self._experts is None:
                self._experts = [{} for _ in range(self.block_count)]

            self._experts[bid][name] = data_torch

            if len(self._experts[bid]) >= n_experts * 3:
                tensors: list[tuple[str, Tensor]] = []

                # merge the experts into a single 3d tensor
                for w_name in ["gate_proj", "up_proj", "down_proj"]:
                    datas: list[Tensor] = []

                    for xid in range(n_experts):
                        ename_to_retrieve = f"model.layers.{bid}.mlp.experts.{xid}.{w_name}.weight"
                        datas.append(self._experts[bid][ename_to_retrieve])
                        del self._experts[bid][ename_to_retrieve]

                    data_torch = torch.stack(datas, dim=0)
                    merged_name = f"model.layers.{bid}.mlp.experts.{w_name}.weight"
                    new_name = self.map_tensor_name(merged_name)
                    tensors.append((new_name, data_torch))

                return tensors
            else:
                return []
        return [(self.map_tensor_name(name), data_torch)]

    def prepare_tensors(self):
        super().prepare_tensors()

        if self._experts is not None:
            # flatten `list[dict[str, Tensor]]` into `list[str]`
            experts = [k for d in self._experts for k in d.keys()]
            if len(experts) > 0:
                raise ValueError(f"Unprocessed experts: {experts}")


@ModelBase.register(
    "Qwen2VLModel",
    "Qwen2VLForConditionalGeneration",
    "Qwen2_5_VLForConditionalGeneration",
    "Qwen2_5OmniModel",
)
class Qwen2VLModel(TextModel):
    model_arch = gguf.MODEL_ARCH.QWEN2VL

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        mrope_section = self.hparams["rope_scaling"]["mrope_section"]
        mrope_section += [0] * max(0, 4 - len(mrope_section))
        self.gguf_writer.add_rope_dimension_sections(mrope_section)

    def set_vocab(self):
        try:
            self._set_vocab_sentencepiece()
        except FileNotFoundError:
            self._set_vocab_gpt2()

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused
        if name.startswith("thinker."):
            name = name.replace("thinker.", "")
        if name.startswith("visual") or name.startswith("audio") or \
                name.startswith("talker") or name.startswith("token2wav"):
            # skip multimodal tensors
            return []
        return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("Qwen2VLModel", "Qwen2VLForConditionalGeneration", "Qwen2_5_VLForConditionalGeneration")
class Qwen2VLVisionModel(MmprojModel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        assert self.hparams_vision is not None
        self.hparams_vision["image_size"] = self.hparams_vision.get("image_size", 560)
        # rename config.json values
        self.hparams_vision["num_attention_heads"] = self.hparams_vision.get("num_heads")
        self.hparams_vision["num_hidden_layers"] = self.hparams_vision.get("depth")
        if "embed_dim" in self.hparams_vision: # qwen2vl
            self.hparams_vision["intermediate_size"] = self.hparams_vision.get("hidden_size")
            self.hparams_vision["hidden_size"] = self.hparams_vision.get("embed_dim")

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        assert self.hparams_vision is not None
        hparams = self.hparams_vision
        model_type = self.global_config['model_type']
        if model_type == 'qwen2_vl':
            self.gguf_writer.add_clip_projector_type(gguf.VisionProjectorType.QWEN2VL)
        elif model_type == 'qwen2_5_vl' or model_type == 'qwen2_5_omni':
            if model_type == 'qwen2_5_omni':
                self.gguf_writer.add_clip_projector_type(gguf.VisionProjectorType.QWEN25O)
            else:
                self.gguf_writer.add_clip_projector_type(gguf.VisionProjectorType.QWEN25VL)
            self.gguf_writer.add_vision_use_silu(True)
            # find n_wa_pattern (window attention pattern)
            fullatt_block_indexes = hparams.get("fullatt_block_indexes")
            assert fullatt_block_indexes is not None, "fullatt_block_indexes is required for qwen2_5_vl"
            n_wa_pattern = fullatt_block_indexes[0] + 1
            # validate n_wa_pattern
            for i in range(1, len(fullatt_block_indexes)):
                if fullatt_block_indexes[i] - fullatt_block_indexes[i - 1] != n_wa_pattern:
                    raise ValueError(f"Invalid fullatt_block_indexes: {fullatt_block_indexes}")
            self.gguf_writer.add_vision_n_wa_pattern(n_wa_pattern)
        else:
            raise ValueError(f"Unknown QwenVL model type: {self.global_config['model_type']}")
        # default values below are taken from HF tranformers code
        self.gguf_writer.add_vision_attention_layernorm_eps(self.global_config.get("rms_norm_eps", 1e-6))

    def tensor_force_quant(self, name, new_name, bid, n_dims):
        del bid, name, n_dims  # unused
        if ".patch_embd." in new_name:
            return gguf.GGMLQuantizationType.F16
        if ".position_embd." in new_name:
            return gguf.GGMLQuantizationType.F32
        return False

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused
        if name.startswith("visual."):
            # process visual tensors
            # split QKV tensors if needed
            if ".qkv." in name:
                if data_torch.ndim == 2: # weight
                    c3, _ = data_torch.shape
                else: # bias
                    c3 = data_torch.shape[0]
                assert c3 % 3 == 0
                c = c3 // 3
                wq = data_torch[:c]
                wk = data_torch[c: c * 2]
                wv = data_torch[c * 2:]
                return [
                    (self.map_tensor_name(name.replace("qkv", "q")), wq),
                    (self.map_tensor_name(name.replace("qkv", "k")), wk),
                    (self.map_tensor_name(name.replace("qkv", "v")), wv),
                ]
            elif 'patch_embed.proj.weight' in name:
                # split Conv3D into Conv2Ds
                c1, c2, kt, kh, kw = data_torch.shape
                del c1, c2, kh, kw  # unused
                assert kt == 2, "Current implmentation only support temporal_patch_size of 2"
                return [
                    (gguf.TENSOR_NAMES[gguf.MODEL_TENSOR.V_ENC_EMBD_PATCH] + ".weight"  , data_torch[:, :, 0, ...]),
                    (gguf.TENSOR_NAMES[gguf.MODEL_TENSOR.V_ENC_EMBD_PATCH] + ".weight.1", data_torch[:, :, 1, ...]),
                ]
            else:
                return [(self.map_tensor_name(name), data_torch)]
        return [] # skip other tensors


@ModelBase.register("Qwen2_5OmniModel")
class Qwen25OmniModel(Qwen2VLVisionModel):
    has_vision_encoder = True
    has_audio_encoder = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        assert self.hparams_audio is not None
        self.hparams_audio["hidden_size"] = self.hparams_audio["d_model"]
        self.hparams_audio["intermediate_size"] = self.hparams_audio["encoder_ffn_dim"]
        self.hparams_audio["num_attention_heads"] = self.hparams_audio["encoder_attention_heads"]

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        assert self.hparams_audio is not None
        self.gguf_writer.add_audio_num_mel_bins(self.hparams_audio["num_mel_bins"])
        self.gguf_writer.add_audio_attention_layernorm_eps(self.hparams_audio.get("layer_norm_eps", 1e-5))

    def get_vision_config(self) -> dict[str, Any] | None:
        return self.global_config["thinker_config"].get("vision_config")

    def get_audio_config(self) -> dict[str, Any] | None:
        return self.global_config["thinker_config"].get("audio_config")

    def generate_extra_tensors(self) -> Iterable[tuple[str, Tensor]]:
        # SinusoidsPositionEmbedding
        assert self.hparams_audio is not None
        max_timescale = 10000
        length = 1500
        channels = self.hparams_audio["hidden_size"]
        log_timescale_increment = np.log(max_timescale) / (channels // 2 - 1)
        inv_timescales = torch.exp(-log_timescale_increment * torch.arange(channels // 2).float())
        scaled_time = torch.arange(length)[:, np.newaxis] * inv_timescales[np.newaxis, :]
        pos_embd = torch.cat([torch.sin(scaled_time), torch.cos(scaled_time)], dim=1).to(dtype=torch.float32)
        yield ("audio_tower.embed_positions.weight", pos_embd)

    def tensor_force_quant(self, name, new_name, bid, n_dims):
        del bid, new_name, n_dims  # unused
        if ".conv" in name and ".weight" in name:
            return gguf.GGMLQuantizationType.F16
        return False

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        if name.startswith("thinker."):
            name = name.replace("thinker.", "")

        if name.startswith("audio_tower"):
            # process audio tensors
            if "conv1.bias" in name or "conv2.bias" in name:
                # transpose conv1 and conv2 bias
                data_torch = data_torch.unsqueeze(-1)
            if "audio_bos_eos_token" in name:
                # this tensor is left unused in transformers code
                # https://github.com/huggingface/transformers/blob/6e3063422c4b1c014aa60c32b9254fd2902f0f28/src/transformers/models/qwen2_5_omni/modular_qwen2_5_omni.py#L1809
                return []
            return [(self.map_tensor_name(name), data_torch)]

        return super().modify_tensors(data_torch, name, bid)


@ModelBase.register("InternVisionModel")
class InternVisionModel(MmprojModel):
    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        hparams = self.hparams
        self.gguf_writer.add_clip_projector_type(gguf.VisionProjectorType.INTERNVL)
        self.gguf_writer.add_vision_attention_layernorm_eps(hparams["layer_norm_eps"])
        # hidden_act
        if hparams["hidden_act"] == "silu":
            self.gguf_writer.add_vision_use_silu(True)
        elif hparams["hidden_act"] == "gelu":
            self.gguf_writer.add_vision_use_gelu(True)
        else:
            raise ValueError(f"Unsupported hidden_act: {hparams['hidden_act']}")
        # downsample_ratio
        downsample_ratio = self.global_config.get("downsample_ratio")
        assert downsample_ratio is not None
        self.gguf_writer.add_vision_projector_scale_factor(int(1.0 / downsample_ratio))

    def tensor_force_quant(self, name, new_name, bid, n_dims):
        del bid, name, n_dims  # unused
        if ".patch_embd." in new_name:
            return gguf.GGMLQuantizationType.F16
        if ".position_embd." in new_name:
            return gguf.GGMLQuantizationType.F32
        return False

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused
        if name.startswith("vision_model") or name.startswith("mlp"):
            # process visual tensors
            # correct name
            if name.startswith("vision_model"):
                name = "vision_tower." + name
            if (".ls" in name or "position_embedding" in name) and not name.endswith(".weight"):
                name += ".weight"
            # split QKV tensors if needed
            if ".qkv." in name:
                if data_torch.ndim == 2: # weight
                    c3, _ = data_torch.shape
                else: # bias
                    c3 = data_torch.shape[0]
                assert c3 % 3 == 0
                c = c3 // 3
                wq = data_torch[:c]
                wk = data_torch[c: c * 2]
                wv = data_torch[c * 2:]
                return [
                    (self.map_tensor_name(name.replace("attn.qkv", "self_attn.q_proj")), wq),
                    (self.map_tensor_name(name.replace("attn.qkv", "self_attn.k_proj")), wk),
                    (self.map_tensor_name(name.replace("attn.qkv", "self_attn.v_proj")), wv),
                ]
            return [(self.map_tensor_name(name), data_torch)]
        return [] # skip other tensors


@ModelBase.register("WavTokenizerDec")
class WavTokenizerDecModel(TextModel):
    model_arch = gguf.MODEL_ARCH.WAVTOKENIZER_DEC

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        if \
                name.endswith("codebook.cluster_size") or \
                name.endswith("codebook.embed_avg") or \
                name.endswith("codebook.inited"):
            logger.debug(f"Skipping {name!r}")
            return []

        logger.info(f"{self.map_tensor_name(name)} -> {data_torch.shape}")

        return [(self.map_tensor_name(name), data_torch)]

    def set_vocab(self):
        self._set_vocab_none()

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_vocab_size         (self.hparams["vocab_size"])
        self.gguf_writer.add_features_length    (self.hparams["n_embd_features"])
        self.gguf_writer.add_feed_forward_length(self.hparams["n_ff"])
        self.gguf_writer.add_group_norm_eps     (self.hparams["group_norm_epsilon"])
        self.gguf_writer.add_group_norm_groups  (self.hparams["group_norm_groups"])

        self.gguf_writer.add_posnet_embedding_length(self.hparams["posnet"]["n_embd"])
        self.gguf_writer.add_posnet_block_count     (self.hparams["posnet"]["n_layer"])

        self.gguf_writer.add_convnext_embedding_length(self.hparams["convnext"]["n_embd"])
        self.gguf_writer.add_convnext_block_count     (self.hparams["convnext"]["n_layer"])

        self.gguf_writer.add_causal_attention(False)


@ModelBase.register("Qwen2MoeForCausalLM")
class Qwen2MoeModel(TextModel):
    model_arch = gguf.MODEL_ARCH.QWEN2MOE

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        if (n_experts := self.hparams.get("num_experts")) is not None:
            self.gguf_writer.add_expert_count(n_experts)
        if (moe_intermediate_size := self.hparams.get("moe_intermediate_size")) is not None:
            self.gguf_writer.add_expert_feed_forward_length(moe_intermediate_size)
            logger.info(f"gguf: expert feed forward length = {moe_intermediate_size}")
        if (shared_expert_intermediate_size := self.hparams.get('shared_expert_intermediate_size')) is not None:
            self.gguf_writer.add_expert_shared_feed_forward_length(shared_expert_intermediate_size)
            logger.info(f"gguf: expert shared feed forward length = {shared_expert_intermediate_size}")
        # YaRN is not enabled by default
        # To enable it, please refer to this guide: https://huggingface.co/Qwen/Qwen3-30B-A3B#processing-long-texts
        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "yarn" and "factor" in rope_scaling:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.YARN)
            self.gguf_writer.add_rope_scaling_factor(rope_scaling["factor"])
            self.gguf_writer.add_rope_scaling_orig_ctx_len(rope_scaling["original_max_position_embeddings"])

    _experts: list[dict[str, Tensor]] | None = None

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        # process the experts separately
        if name.find("experts") != -1:
            n_experts = self.hparams["num_experts"]
            assert bid is not None

            if self._experts is None:
                self._experts = [{} for _ in range(self.block_count)]

            self._experts[bid][name] = data_torch

            if len(self._experts[bid]) >= n_experts * 3:
                tensors: list[tuple[str, Tensor]] = []

                # merge the experts into a single 3d tensor
                for w_name in ["down_proj", "gate_proj", "up_proj"]:
                    datas: list[Tensor] = []

                    for xid in range(n_experts):
                        ename = f"model.layers.{bid}.mlp.experts.{xid}.{w_name}.weight"
                        datas.append(self._experts[bid][ename])
                        del self._experts[bid][ename]

                    data_torch = torch.stack(datas, dim=0)

                    merged_name = f"model.layers.{bid}.mlp.experts.{w_name}.weight"

                    new_name = self.map_tensor_name(merged_name)

                    tensors.append((new_name, data_torch))
                return tensors
            else:
                return []

        return [(self.map_tensor_name(name), data_torch)]

    def prepare_tensors(self):
        super().prepare_tensors()

        if self._experts is not None:
            # flatten `list[dict[str, Tensor]]` into `list[str]`
            experts = [k for d in self._experts for k in d.keys()]
            if len(experts) > 0:
                raise ValueError(f"Unprocessed experts: {experts}")


@ModelBase.register("Qwen3ForCausalLM")
class Qwen3Model(Qwen2Model):
    model_arch = gguf.MODEL_ARCH.QWEN3


@ModelBase.register("Qwen3MoeForCausalLM")
class Qwen3MoeModel(Qwen2MoeModel):
    model_arch = gguf.MODEL_ARCH.QWEN3MOE


@ModelBase.register("GPT2LMHeadModel")
class GPT2Model(TextModel):
    model_arch = gguf.MODEL_ARCH.GPT2

    def set_gguf_parameters(self):
        self.gguf_writer.add_block_count(self.hparams["n_layer"])
        self.gguf_writer.add_context_length(self.hparams["n_ctx"])
        self.gguf_writer.add_embedding_length(self.hparams["n_embd"])
        self.gguf_writer.add_feed_forward_length(4 * self.hparams["n_embd"])
        self.gguf_writer.add_head_count(self.hparams["n_head"])
        self.gguf_writer.add_layer_norm_eps(self.hparams["layer_norm_epsilon"])
        self.gguf_writer.add_file_type(self.ftype)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        tensors: list[tuple[str, Tensor]] = []

        # we don't need these
        if name.endswith((".attn.bias", ".attn.masked_bias")):
            return tensors

        if name.endswith((".c_attn.weight", ".c_proj.weight", ".c_fc.weight", ".c_proj.weight")):
            data_torch = data_torch.transpose(1, 0)

        new_name = self.map_tensor_name(name)

        tensors.append((new_name, data_torch))

        return tensors


@ModelBase.register("PhiForCausalLM")
class Phi2Model(TextModel):
    model_arch = gguf.MODEL_ARCH.PHI2

    def set_gguf_parameters(self):
        block_count = self.find_hparam(["num_hidden_layers", "n_layer"])

        rot_pct = self.find_hparam(["partial_rotary_factor"])
        n_embd = self.find_hparam(["hidden_size", "n_embd"])
        n_head = self.find_hparam(["num_attention_heads", "n_head"])

        self.gguf_writer.add_context_length(self.find_hparam(["n_positions", "max_position_embeddings"]))

        self.gguf_writer.add_embedding_length(n_embd)
        self.gguf_writer.add_feed_forward_length(4 * n_embd)
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_head_count(n_head)
        self.gguf_writer.add_head_count_kv(n_head)
        self.gguf_writer.add_layer_norm_eps(self.find_hparam(["layer_norm_epsilon", "layer_norm_eps"]))
        self.gguf_writer.add_rope_dimension_count(int(rot_pct * n_embd) // n_head)
        self.gguf_writer.add_file_type(self.ftype)
        self.gguf_writer.add_add_bos_token(False)


@ModelBase.register("Phi3ForCausalLM")
class Phi3MiniModel(TextModel):
    model_arch = gguf.MODEL_ARCH.PHI3

    def set_vocab(self):
        # Phi-4 model uses GPT2Tokenizer
        tokenizer_config_file = self.dir_model / 'tokenizer_config.json'
        if tokenizer_config_file.is_file():
            with open(tokenizer_config_file, "r", encoding="utf-8") as f:
                tokenizer_config_json = json.load(f)
                tokenizer_class = tokenizer_config_json['tokenizer_class']
                if tokenizer_class == 'GPT2Tokenizer':
                    return self._set_vocab_gpt2()

        from sentencepiece import SentencePieceProcessor

        tokenizer_path = self.dir_model / 'tokenizer.model'

        if not tokenizer_path.is_file():
            raise ValueError(f'Error: Missing {tokenizer_path}')

        tokenizer = SentencePieceProcessor()
        tokenizer.LoadFromFile(str(tokenizer_path))

        vocab_size = self.hparams.get('vocab_size', tokenizer.vocab_size())

        tokens: list[bytes] = [f"[PAD{i}]".encode("utf-8") for i in range(vocab_size)]
        scores: list[float] = [-10000.0] * vocab_size
        toktypes: list[int] = [SentencePieceTokenTypes.UNUSED] * vocab_size

        for token_id in range(tokenizer.vocab_size()):

            piece = tokenizer.IdToPiece(token_id)
            text = piece.encode("utf-8")
            score = tokenizer.GetScore(token_id)

            toktype = SentencePieceTokenTypes.NORMAL
            if tokenizer.IsUnknown(token_id):
                toktype = SentencePieceTokenTypes.UNKNOWN
            elif tokenizer.IsControl(token_id):
                toktype = SentencePieceTokenTypes.CONTROL
            elif tokenizer.IsUnused(token_id):
                toktype = SentencePieceTokenTypes.UNUSED
            elif tokenizer.IsByte(token_id):
                toktype = SentencePieceTokenTypes.BYTE

            tokens[token_id] = text
            scores[token_id] = score
            toktypes[token_id] = toktype

        added_tokens_file = self.dir_model / 'added_tokens.json'
        if added_tokens_file.is_file():
            with open(added_tokens_file, "r", encoding="utf-8") as f:
                added_tokens_json = json.load(f)

                for key in added_tokens_json:
                    token_id = added_tokens_json[key]
                    if token_id >= vocab_size:
                        logger.debug(f'ignore token {token_id}: id is out of range, max={vocab_size - 1}')
                        continue

                    tokens[token_id] = key.encode("utf-8")
                    scores[token_id] = -1000.0
                    toktypes[token_id] = SentencePieceTokenTypes.USER_DEFINED

        tokenizer_config_file = self.dir_model / 'tokenizer_config.json'
        if tokenizer_config_file.is_file():
            with open(tokenizer_config_file, "r", encoding="utf-8") as f:
                tokenizer_config_json = json.load(f)
                added_tokens_decoder = tokenizer_config_json.get("added_tokens_decoder", {})
                for token_id, foken_data in added_tokens_decoder.items():
                    token_id = int(token_id)
                    token = foken_data["content"].encode("utf-8")
                    if toktypes[token_id] != SentencePieceTokenTypes.UNUSED:
                        if tokens[token_id] != token:
                            logger.warning(f'replacing token {token_id}: {tokens[token_id].decode("utf-8")!r} -> {token.decode("utf-8")!r}')
                    tokens[token_id] = token
                    scores[token_id] = -1000.0
                    toktypes[token_id] = SentencePieceTokenTypes.USER_DEFINED
                    if foken_data.get("special"):
                        toktypes[token_id] = SentencePieceTokenTypes.CONTROL

        tokenizer_file = self.dir_model / 'tokenizer.json'
        if tokenizer_file.is_file():
            with open(tokenizer_file, "r", encoding="utf-8") as f:
                tokenizer_json = json.load(f)
                added_tokens = tokenizer_json.get("added_tokens", [])
                for foken_data in added_tokens:
                    token_id = int(foken_data["id"])
                    token = foken_data["content"].encode("utf-8")
                    if toktypes[token_id] != SentencePieceTokenTypes.UNUSED:
                        if tokens[token_id] != token:
                            logger.warning(f'replacing token {token_id}: {tokens[token_id].decode("utf-8")!r} -> {token.decode("utf-8")!r}')
                    tokens[token_id] = token
                    scores[token_id] = -1000.0
                    toktypes[token_id] = SentencePieceTokenTypes.USER_DEFINED
                    if foken_data.get("special"):
                        toktypes[token_id] = SentencePieceTokenTypes.CONTROL

        self.gguf_writer.add_tokenizer_model("llama")
        self.gguf_writer.add_tokenizer_pre("default")
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_scores(scores)
        self.gguf_writer.add_token_types(toktypes)

        special_vocab = gguf.SpecialVocab(self.dir_model, n_vocab=len(tokens))
        special_vocab.add_to_gguf(self.gguf_writer)

    def set_gguf_parameters(self):
        block_count = self.find_hparam(["num_hidden_layers", "n_layer"])

        n_embd = self.find_hparam(["hidden_size", "n_embd"])
        n_head = self.find_hparam(["num_attention_heads", "n_head"])
        n_head_kv = self.find_hparam(["num_key_value_heads", "n_head_kv"])
        rms_eps = self.find_hparam(["rms_norm_eps"])
        max_pos_embds = self.find_hparam(["n_positions", "max_position_embeddings"])
        orig_max_pos_embds = self.find_hparam(["original_max_position_embeddings"])
        rot_pct = self.hparams.get("partial_rotary_factor", 1.0)
        rope_dims = int(rot_pct * n_embd) // n_head

        self.gguf_writer.add_context_length(max_pos_embds)
        self.gguf_writer.add_rope_scaling_orig_ctx_len(orig_max_pos_embds)
        self.gguf_writer.add_embedding_length(n_embd)
        self.gguf_writer.add_feed_forward_length(self.find_hparam(["intermediate_size"]))
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_head_count(n_head)
        self.gguf_writer.add_head_count_kv(n_head_kv)
        self.gguf_writer.add_layer_norm_rms_eps(rms_eps)
        self.gguf_writer.add_rope_dimension_count(rope_dims)
        self.gguf_writer.add_rope_freq_base(self.find_hparam(["rope_theta"]))
        self.gguf_writer.add_file_type(self.ftype)
        sliding_window = self.hparams.get("sliding_window")
        # use zero value of sliding_window to distinguish Phi-4 from other PHI3 models
        if sliding_window is None:
            sliding_window = 0
        self.gguf_writer.add_sliding_window(sliding_window)

    def generate_extra_tensors(self) -> Iterable[tuple[str, Tensor]]:
        n_embd = self.find_hparam(["hidden_size", "n_embd"])
        n_head = self.find_hparam(["num_attention_heads", "n_head"])
        max_pos_embds = self.find_hparam(["n_positions", "max_position_embeddings"])
        orig_max_pos_embds = self.find_hparam(["original_max_position_embeddings"])
        rot_pct = self.hparams.get("partial_rotary_factor", 1.0)
        rope_dims = int(rot_pct * n_embd) // n_head

        # write rope scaling for long context (128k) model
        rope_scaling = self.find_hparam(['rope_scaling'], True)
        if rope_scaling is None:
            return

        scale = max_pos_embds / orig_max_pos_embds

        rope_scaling_type = rope_scaling.get('rope_type', rope_scaling.get('type', '')).lower()
        if len(rope_scaling_type) == 0:
            raise KeyError('Missing the required key rope_scaling.type')

        if rope_scaling_type == 'su' or rope_scaling_type == 'longrope':
            attn_factor = math.sqrt(1 + math.log(scale) / math.log(orig_max_pos_embds)) if scale > 1.0 else 1.0
        elif rope_scaling_type == 'yarn':
            attn_factor = 0.1 * math.log(scale) + 1.0 if scale > 1.0 else 1.0
        else:
            raise NotImplementedError(f'The rope scaling type {rope_scaling_type} is not supported yet')

        self.gguf_writer.add_rope_scaling_attn_factors(attn_factor)

        long_factors = rope_scaling.get('long_factor', None)
        short_factors = rope_scaling.get('short_factor', None)

        if long_factors is None or short_factors is None:
            raise KeyError('Missing the required key rope_scaling.long_factor or rope_scaling_short_factor')

        if len(long_factors) != len(short_factors) or len(long_factors) != rope_dims / 2:
            raise ValueError(f'The length of rope long and short factors must be {rope_dims / 2}. long_factors = {len(long_factors)}, short_factors = {len(short_factors)}.')

        yield (self.format_tensor_name(gguf.MODEL_TENSOR.ROPE_FACTORS_LONG), torch.tensor(long_factors, dtype=torch.float32))
        yield (self.format_tensor_name(gguf.MODEL_TENSOR.ROPE_FACTORS_SHORT), torch.tensor(short_factors, dtype=torch.float32))


@ModelBase.register("PhiMoEForCausalLM")
class PhiMoeModel(Phi3MiniModel):
    model_arch = gguf.MODEL_ARCH.PHIMOE

    _experts: list[dict[str, Tensor]] | None = None

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_expert_used_count(self.hparams["num_experts_per_tok"])
        self.gguf_writer.add_expert_count(self.hparams["num_local_experts"])

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        # process the experts separately
        if name.find("block_sparse_moe.experts") != -1:
            n_experts = self.hparams["num_local_experts"]
            assert bid is not None

            if self._experts is None:
                self._experts = [{} for _ in range(self.block_count)]

            self._experts[bid][name] = data_torch

            if len(self._experts[bid]) >= n_experts * 3:
                tensors: list[tuple[str, Tensor]] = []

                # merge the experts into a single 3d tensor
                for w_name in ["w1", "w2", "w3"]:
                    datas: list[Tensor] = []

                    for xid in range(n_experts):
                        ename = f"model.layers.{bid}.block_sparse_moe.experts.{xid}.{w_name}.weight"
                        datas.append(self._experts[bid][ename])
                        del self._experts[bid][ename]

                    data_torch = torch.stack(datas, dim=0)

                    merged_name = f"model.layers.{bid}.block_sparse_moe.experts.{w_name}.weight"

                    new_name = self.map_tensor_name(merged_name)

                    tensors.append((new_name, data_torch))
                return tensors
            else:
                return []

        return [(self.map_tensor_name(name), data_torch)]

    def prepare_tensors(self):
        super().prepare_tensors()

        if self._experts is not None:
            # flatten `list[dict[str, Tensor]]` into `list[str]`
            experts = [k for d in self._experts for k in d.keys()]
            if len(experts) > 0:
                raise ValueError(f"Unprocessed experts: {experts}")


@ModelBase.register("PlamoForCausalLM")
class PlamoModel(TextModel):
    model_arch = gguf.MODEL_ARCH.PLAMO

    def set_vocab(self):
        self._set_vocab_sentencepiece()

    def set_gguf_parameters(self):
        hparams = self.hparams
        block_count = hparams["num_hidden_layers"]

        self.gguf_writer.add_context_length(4096)  # not in config.json
        self.gguf_writer.add_embedding_length(hparams["hidden_size"])
        self.gguf_writer.add_feed_forward_length(hparams["intermediate_size"])
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_head_count(hparams["num_attention_heads"])
        self.gguf_writer.add_head_count_kv(5)  # hparams["num_key_value_heads"]) is wrong
        self.gguf_writer.add_layer_norm_rms_eps(hparams["rms_norm_eps"])
        self.gguf_writer.add_file_type(self.ftype)

    def shuffle_attn_q_weight(self, data_torch):
        assert data_torch.size() == (5120, 5120)
        data_torch = data_torch.reshape(8, 5, 128, 5120)
        data_torch = torch.permute(data_torch, (1, 0, 2, 3))
        data_torch = torch.reshape(data_torch, (5120, 5120))
        return data_torch

    def shuffle_attn_output_weight(self, data_torch):
        assert data_torch.size() == (5120, 5120)
        data_torch = data_torch.reshape(5120, 8, 5, 128)
        data_torch = torch.permute(data_torch, (0, 2, 1, 3))
        data_torch = torch.reshape(data_torch, (5120, 5120))
        return data_torch

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        new_name = self.map_tensor_name(name)

        # shuffle for broadcasting of gqa in ggml_mul_mat
        if new_name.endswith("attn_q.weight"):
            data_torch = self.shuffle_attn_q_weight(data_torch)
        elif new_name.endswith("attn_output.weight"):
            data_torch = self.shuffle_attn_output_weight(data_torch)

        return [(new_name, data_torch)]


@ModelBase.register("Plamo2ForCausalLM", "PLaMo2ForCausalLM")
class Plamo2Model(TextModel):
    model_arch = gguf.MODEL_ARCH.PLAMO2

    def set_vocab(self):
        # PLaMo 2 uses a custom tokenizer with a .jsonl file
        # We need to handle this specially
        tokenizer_jsonl_path = self.dir_model / "tokenizer.jsonl"
        tokenizer_config_path = self.dir_model / "tokenizer_config.json"

        if not tokenizer_jsonl_path.is_file():
            raise FileNotFoundError(f"PLaMo 2 tokenizer file not found: {tokenizer_jsonl_path}")

        # Load tokenizer config
        with open(tokenizer_config_path, 'r', encoding='utf-8') as f:
            tokenizer_config = json.load(f)

        # Load tokens from JSONL file (actually a list format)
        tokens = []
        scores = []
        toktypes = []

        with open(tokenizer_jsonl_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f):
                if line.strip():
                    token_data = json.loads(line)
                    # Format: [token, score, type, ?, ?, ?, ?]
                    token = token_data[0].encode("utf-8")
                    score = float(token_data[1])
                    token_type_str = token_data[2] if len(token_data) > 2 else "NORMAL"

                    tokens.append(token)
                    scores.append(score)

                    # Map token type strings to GGUF token types
                    if token_type_str == "UNKNOWN":
                        toktypes.append(gguf.TokenType.UNKNOWN)
                    elif token_type_str == "CONTROL":
                        toktypes.append(gguf.TokenType.CONTROL)
                    elif token_type_str == "BYTE":
                        toktypes.append(gguf.TokenType.BYTE)
                    else:
                        # Check for PLaMo-2 special tokens
                        token_str = token_data[0]
                        if token_str.startswith("<|plamo:") and token_str.endswith("|>"):
                            toktypes.append(gguf.TokenType.CONTROL)
                        else:
                            toktypes.append(gguf.TokenType.NORMAL)

        vocab_size = self.hparams["vocab_size"]
        if vocab_size > len(tokens):
            pad_count = vocab_size - len(tokens)
            logger.debug(f"Padding vocab with {pad_count} token(s) - [PAD1] through [PAD{pad_count}]")
            for i in range(1, pad_count + 1):
                tokens.append(bytes(f"[PAD{i}]", encoding="utf-8"))
                scores.append(-1000.0)
                toktypes.append(gguf.TokenType.UNUSED)

        # Use "plamo2" tokenizer type for PLaMo-2's custom Aho-Corasick tokenizer
        self.gguf_writer.add_tokenizer_model("plamo2")
        self.gguf_writer.add_tokenizer_pre("default")
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_scores(scores)
        self.gguf_writer.add_token_types(toktypes)

        # Add special tokens from config
        if "bos_token" in tokenizer_config and tokenizer_config["bos_token"] is not None:
            token_id = tokens.index(tokenizer_config["bos_token"].encode("utf-8"))
            self.gguf_writer.add_bos_token_id(token_id)
        if "eos_token" in tokenizer_config and tokenizer_config["eos_token"] is not None:
            token_id = tokens.index(tokenizer_config["eos_token"].encode("utf-8"))
            self.gguf_writer.add_eos_token_id(token_id)
        if "pad_token" in tokenizer_config and tokenizer_config["pad_token"] is not None:
            token_id = tokens.index(tokenizer_config["pad_token"].encode("utf-8"))
            self.gguf_writer.add_pad_token_id(token_id)
        if "sep_token" in tokenizer_config and tokenizer_config["sep_token"] is not None:
            token_id = tokens.index(tokenizer_config["sep_token"].encode("utf-8"))
            self.gguf_writer.add_sep_token_id(token_id)
        if "unk_token" in tokenizer_config and tokenizer_config["unk_token"] is not None:
            token_id = tokens.index(tokenizer_config["unk_token"].encode("utf-8"))
            self.gguf_writer.add_unk_token_id(token_id)

        # Add <|plamo:op|> as EOT to ensure appropriate end of generation
        self.gguf_writer.add_eot_token_id(4)

        self.gguf_writer.add_add_space_prefix(False)

    def set_gguf_parameters(self):
        hparams = self.hparams
        block_count = hparams["num_hidden_layers"]
        self.gguf_writer.add_vocab_size(self.hparams["vocab_size"])

        # Which layers are Mamba layers
        # PLaMo 2 uses mamba_step to indicate the pattern (e.g., 2 means every other layer)
        # This logic matches modeling_plamo.py's is_mamba function
        mamba_step = hparams.get("mamba_step", 2)
        mamba_enabled = hparams.get("mamba_enabled", True)
        mamba_layers = []

        if mamba_enabled:
            for i in range(block_count):
                if block_count <= (mamba_step // 2):
                    # use attention in last layer
                    is_mamba = (i != block_count - 1)
                else:
                    is_mamba = (i % mamba_step) != (mamba_step // 2)
                if is_mamba:
                    mamba_layers.append(0)
                else:
                    mamba_layers.append(hparams.get("num_key_value_heads", 4))

        if mamba_layers:
            self.gguf_writer.add_head_count_kv(mamba_layers)

        self.gguf_writer.add_context_length(hparams.get("max_position_embeddings", 2048))
        self.gguf_writer.add_embedding_length(hparams.get("hidden_size", 4096))
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_head_count(hparams.get("num_attention_heads", 32))
        self.gguf_writer.add_layer_norm_rms_eps(hparams.get("rms_norm_eps", 1e-06))
        self.gguf_writer.add_rope_freq_base(hparams.get("rope_theta", 1000000.0))

        # Mamba parameters
        self.gguf_writer.add_ssm_state_size(hparams.get("mamba_d_state", 64))
        self.gguf_writer.add_ssm_conv_kernel(hparams.get("mamba_d_conv", 4))
        self.gguf_writer.add_ssm_time_step_rank(hparams.get("mamba_num_heads", 64))
        intermediate_size = hparams.get("mamba_num_heads", 64) * hparams.get("hidden_size_per_head", 128)
        self.gguf_writer.add_ssm_inner_size(intermediate_size)
        self.gguf_writer.add_ssm_group_count(0)

        # MLP feed forward parameters (for attention layers)
        self.gguf_writer.add_feed_forward_length(hparams.get("intermediate_size", 16384))
        self.gguf_writer.add_file_type(self.ftype)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        if name.endswith(".A_log"):
            data_torch = -torch.exp(data_torch)
        elif name.endswith(".dt_bias"):
            name = name.rpartition(".dt_bias")[0] + ".dt_proj.bias"
        elif name.endswith(".dt_norm_weight"):
            name = name.rpartition(".dt_norm_weight")[0] + ".dt_norm.weight"
        elif name.endswith(".B_norm_weight"):
            name = name.rpartition(".B_norm_weight")[0] + ".B_norm.weight"
        elif name.endswith(".C_norm_weight"):
            name = name.rpartition(".C_norm_weight")[0] + ".C_norm.weight"
        elif name.endswith(".k_weight"):
            name = name.rpartition(".k_weight")[0] + ".k.weight"
        elif name.endswith(".q_weight"):
            name = name.rpartition(".q_weight")[0] + ".q.weight"
        elif name.endswith(".conv1d.weight"):
            data_torch = torch.squeeze(data_torch)  # remove (, 1, )
            assert data_torch.ndim == 2
        elif name.endswith(".pre_mixer_norm.weight"):
            data_torch += 1.0
        elif name.endswith(".post_mixer_norm.weight"):
            data_torch += 1.0 / 5
        elif name.endswith(".pre_mlp_norm.weight"):
            data_torch += 1.0
        elif name.endswith(".post_mlp_norm.weight"):
            data_torch += 1.0 / (5**1.5)
        elif name.endswith(".norm.weight"):
            data_torch += 1.0

        new_name = self.map_tensor_name(name)

        return [(new_name, data_torch)]


@ModelBase.register("CodeShellForCausalLM")
class CodeShellModel(TextModel):
    model_arch = gguf.MODEL_ARCH.CODESHELL

    def set_gguf_parameters(self):
        block_count = self.hparams["n_layer"]

        self.gguf_writer.add_context_length(self.hparams["n_positions"])
        self.gguf_writer.add_embedding_length(self.hparams["n_embd"])
        self.gguf_writer.add_feed_forward_length(4 * self.hparams["n_embd"])
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_head_count(self.hparams["n_head"])
        self.gguf_writer.add_head_count_kv(self.hparams["num_query_groups"])
        self.gguf_writer.add_layer_norm_eps(self.hparams["layer_norm_epsilon"])
        self.gguf_writer.add_file_type(self.ftype)
        self.gguf_writer.add_rope_freq_base(10000.0)
        self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.LINEAR)
        self.gguf_writer.add_rope_scaling_factor(1.0)

    _has_tok_embd = False

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        output_name = self.format_tensor_name(gguf.MODEL_TENSOR.OUTPUT)
        tok_embd_name = self.format_tensor_name(gguf.MODEL_TENSOR.TOKEN_EMBD)

        new_name = self.map_tensor_name(name)

        # assuming token_embd.weight is seen before output.weight
        if not self._has_tok_embd and new_name == self.format_tensor_name(gguf.MODEL_TENSOR.OUTPUT):
            # even though the tensor file(s) does not contain the word embeddings they are still in the weight map
            if self.tensor_names and "transformer.wte.weight" in self.tensor_names:
                logger.debug(f"{tok_embd_name} not found before {output_name}, assuming they are tied")
                self.tensor_names.remove("transformer.wte.weight")
        elif new_name == tok_embd_name:
            self._has_tok_embd = True

        return [(new_name, data_torch)]


@ModelBase.register("InternLM2ForCausalLM")
class InternLM2Model(TextModel):
    model_arch = gguf.MODEL_ARCH.INTERNLM2

    def set_vocab(self):
        # (TODO): Is there a better way?
        # Copy from _set_vocab_sentencepiece, The only difference is that we will treat the character
        # \x00 specially and convert it into an emoji character to prevent it from being mistakenly
        # recognized as an empty string in C++.
        from sentencepiece import SentencePieceProcessor
        from sentencepiece import sentencepiece_model_pb2 as model

        tokenizer_path = self.dir_model / 'tokenizer.model'

        tokens: list[bytes] = []
        scores: list[float] = []
        toktypes: list[int] = []

        if not tokenizer_path.is_file():
            logger.error(f'Error: Missing {tokenizer_path}')
            sys.exit(1)

        sentencepiece_model = model.ModelProto()  # pyright: ignore[reportAttributeAccessIssue]
        sentencepiece_model.ParseFromString(open(tokenizer_path, "rb").read())
        add_prefix = sentencepiece_model.normalizer_spec.add_dummy_prefix

        tokenizer = SentencePieceProcessor()
        tokenizer.LoadFromFile(str(tokenizer_path))

        vocab_size = self.hparams.get('vocab_size', tokenizer.vocab_size())

        for token_id in range(vocab_size):
            piece = tokenizer.IdToPiece(token_id)
            text = piece.encode("utf-8")
            score = tokenizer.GetScore(token_id)
            if text == b"\x00":
                # (TODO): fixme
                # Hack here and replace the \x00 characters.
                logger.warning(f"InternLM2 convert token '{text}' to '🐉'!")
                text = "🐉".encode("utf-8")

            toktype = SentencePieceTokenTypes.NORMAL
            if tokenizer.IsUnknown(token_id):
                toktype = SentencePieceTokenTypes.UNKNOWN
            elif tokenizer.IsControl(token_id):
                toktype = SentencePieceTokenTypes.CONTROL
            elif tokenizer.IsUnused(token_id):
                toktype = SentencePieceTokenTypes.UNUSED
            elif tokenizer.IsByte(token_id):
                toktype = SentencePieceTokenTypes.BYTE
            # take care of ununsed raw token
            if piece.startswith('[UNUSED'):
                toktype = SentencePieceTokenTypes.UNUSED

            tokens.append(text)
            scores.append(score)
            toktypes.append(toktype)

        added_tokens_file = self.dir_model / 'added_tokens.json'
        if added_tokens_file.is_file():
            with open(added_tokens_file, "r", encoding="utf-8") as f:
                added_tokens_json = json.load(f)

                for key in added_tokens_json:
                    tokens.append(key.encode("utf-8"))
                    scores.append(-1000.0)
                    toktypes.append(SentencePieceTokenTypes.USER_DEFINED)

        chat_eos_token = '<|im_end|>'
        chat_eos_token_id = None

        tokenizer_config_file = self.dir_model / 'tokenizer_config.json'
        if tokenizer_config_file.is_file():
            with open(tokenizer_config_file, "r", encoding="utf-8") as f:
                tokenizer_config_json = json.load(f)
                added_tokens_decoder = tokenizer_config_json.get("added_tokens_decoder", {})
                for token_id, foken_data in added_tokens_decoder.items():
                    token_id = int(token_id)
                    token = foken_data["content"]
                    if token == chat_eos_token:
                        chat_eos_token_id = token_id
                    token = token.encode("utf-8")
                    if toktypes[token_id] != SentencePieceTokenTypes.UNUSED:
                        if tokens[token_id] != token:
                            logger.warning(f'replacing token {token_id}: {tokens[token_id].decode("utf-8")!r} -> {token.decode("utf-8")!r}')
                    tokens[token_id] = token
                    scores[token_id] = -1000.0
                    toktypes[token_id] = SentencePieceTokenTypes.USER_DEFINED
                    if foken_data.get("special"):
                        toktypes[token_id] = SentencePieceTokenTypes.CONTROL

        tokenizer_file = self.dir_model / 'tokenizer.json'
        if tokenizer_file.is_file():
            with open(tokenizer_file, "r", encoding="utf-8") as f:
                tokenizer_json = json.load(f)
                added_tokens = tokenizer_json.get("added_tokens", [])
                for foken_data in added_tokens:
                    token_id = int(foken_data["id"])
                    token = foken_data["content"]
                    if token == chat_eos_token:
                        chat_eos_token_id = token_id
                    token = token.encode("utf-8")
                    if toktypes[token_id] != SentencePieceTokenTypes.UNUSED:
                        if tokens[token_id] != token:
                            logger.warning(f'replacing token {token_id}: {tokens[token_id].decode("utf-8")!r} -> {token.decode("utf-8")!r}')
                    tokens[token_id] = token
                    scores[token_id] = -1000.0
                    toktypes[token_id] = SentencePieceTokenTypes.USER_DEFINED
                    if foken_data.get("special"):
                        toktypes[token_id] = SentencePieceTokenTypes.CONTROL

        self.gguf_writer.add_tokenizer_model("llama")
        self.gguf_writer.add_tokenizer_pre("default")
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_scores(scores)
        self.gguf_writer.add_token_types(toktypes)
        self.gguf_writer.add_add_space_prefix(add_prefix)

        special_vocab = gguf.SpecialVocab(self.dir_model, n_vocab=len(tokens))
        old_eos = special_vocab.special_token_ids["eos"]
        if chat_eos_token_id is not None:
            # For the chat model, we replace the eos with '<|im_end|>'.
            # TODO: this is a hack, should be fixed
            #       https://github.com/ggml-org/llama.cpp/pull/6745#issuecomment-2067687048
            special_vocab.special_token_ids["eos"] = chat_eos_token_id
            logger.warning(f"Replace eos:{old_eos} with a special token:{chat_eos_token_id}"
                           " in chat mode so that the conversation can end normally.")

        special_vocab.add_to_gguf(self.gguf_writer)

    def set_gguf_parameters(self):
        self.gguf_writer.add_context_length(self.hparams["max_position_embeddings"])
        self.gguf_writer.add_block_count(self.hparams["num_hidden_layers"])
        self.gguf_writer.add_embedding_length(self.hparams["hidden_size"])
        self.gguf_writer.add_feed_forward_length(self.hparams["intermediate_size"])
        self.gguf_writer.add_rope_freq_base(self.hparams["rope_theta"])
        self.gguf_writer.add_head_count(self.hparams["num_attention_heads"])
        self.gguf_writer.add_layer_norm_rms_eps(self.hparams["rms_norm_eps"])
        self.gguf_writer.add_head_count_kv(self.hparams["num_key_value_heads"])
        self.gguf_writer.add_file_type(self.ftype)
        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "linear" and "factor" in rope_scaling:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.LINEAR)
            self.gguf_writer.add_rope_scaling_factor(rope_scaling["factor"])

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        num_heads = self.hparams["num_attention_heads"]
        num_kv_heads = self.hparams["num_key_value_heads"]
        n_embd = self.hparams["hidden_size"]
        q_per_kv = num_heads // num_kv_heads
        head_dim = n_embd // num_heads
        num_groups = num_heads // q_per_kv

        name = name.replace("language_model.", "") # InternVL
        if name.startswith("mlp") or name.startswith("vision_model"):
            # skip visual tensors
            return []

        if bid is not None and f"model.layers.{bid}.attention.wqkv" in name:
            qkv = data_torch

            qkv = qkv.reshape((num_groups, q_per_kv + 2, head_dim, n_embd))
            q, k, v = qkv[:, : q_per_kv], qkv[:, -2], qkv[:, -1]

            # The model weights of q and k equire additional reshape.
            q = LlamaModel.permute(q.reshape((-1, q.shape[-1])), num_heads, num_heads)
            k = LlamaModel.permute(k.reshape((-1, k.shape[-1])), num_heads, num_kv_heads)
            v = v.reshape((-1, v.shape[-1]))

            return [
                (self.format_tensor_name(gguf.MODEL_TENSOR.ATTN_Q, bid), q),
                (self.format_tensor_name(gguf.MODEL_TENSOR.ATTN_K, bid), k),
                (self.format_tensor_name(gguf.MODEL_TENSOR.ATTN_V, bid), v),
            ]
        else:
            return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("InternLM3ForCausalLM")
class InternLM3Model(TextModel):
    model_arch = gguf.MODEL_ARCH.LLAMA

    def set_vocab(self):
        tokens, scores, toktypes = self._create_vocab_sentencepiece()

        self.gguf_writer.add_tokenizer_model("llama")
        self.gguf_writer.add_tokenizer_pre("default")
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_scores(scores)
        self.gguf_writer.add_token_types(toktypes)

        special_vocab = gguf.SpecialVocab(self.dir_model, n_vocab=len(tokens))

        tokenizer_config_file = self.dir_model / 'tokenizer_config.json'
        if tokenizer_config_file.is_file():
            with open(tokenizer_config_file, "r", encoding="utf-8") as f:
                tokenizer_config_json = json.load(f)
                if "add_prefix_space" in tokenizer_config_json:
                    self.gguf_writer.add_add_space_prefix(tokenizer_config_json["add_prefix_space"])

                if "added_tokens_decoder" in tokenizer_config_json:
                    for token_id, token_data in tokenizer_config_json["added_tokens_decoder"].items():
                        if token_data.get("special"):
                            token_id = int(token_id)
                            token = token_data["content"]
                            special_vocab._set_special_token(token, token_id)
                            # update eos token
                            if token == '<|im_end|>' and "eos" in special_vocab.special_token_ids:
                                special_vocab.special_token_ids["eos"] = token_id

        special_vocab.add_to_gguf(self.gguf_writer)

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        hparams = self.hparams
        self.gguf_writer.add_vocab_size(hparams["vocab_size"])

        if (rope_dim := hparams.get("head_dim")) is None:
            rope_dim = hparams["hidden_size"] // hparams["num_attention_heads"]
        self.gguf_writer.add_rope_dimension_count(rope_dim)

        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "linear" and "factor" in rope_scaling:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.LINEAR)
            self.gguf_writer.add_rope_scaling_factor(rope_scaling["factor"])

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        n_head = self.hparams["num_attention_heads"]
        n_kv_head = self.hparams.get("num_key_value_heads")
        name = name.replace("language_model.", "") # InternVL
        if name.startswith("mlp") or name.startswith("vision_model"):
            # skip visual tensors
            return []
        if name.endswith(("q_proj.weight", "q_proj.bias")):
            data_torch = LlamaModel.permute(data_torch, n_head, n_head)
        if name.endswith(("k_proj.weight", "k_proj.bias")):
            data_torch = LlamaModel.permute(data_torch, n_head, n_kv_head)
        return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("BertModel", "BertForMaskedLM", "CamembertModel", "BertForSequenceClassification")
class BertModel(TextModel):
    model_arch = gguf.MODEL_ARCH.BERT

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.vocab_size = None

        if cls_out_labels := self.hparams.get("id2label"):
            if len(cls_out_labels) == 2 and cls_out_labels[0] == "LABEL_0":
                # Remove dummy labels added by AutoConfig
                cls_out_labels = None
        self.cls_out_labels = cls_out_labels

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_causal_attention(False)
        self._try_set_pooling_type()

        if self.cls_out_labels:
            self.gguf_writer.add_classifier_output_labels([v for k, v in sorted(self.cls_out_labels.items())])

    def set_vocab(self):
        tokens, toktypes, tokpre = self.get_vocab_base()
        self.vocab_size = len(tokens)

        # we need this to validate the size of the token_type embeddings
        # though currently we are passing all zeros to the token_type embeddings
        # "Sequence A" or "Sequence B"
        self.gguf_writer.add_token_type_count(self.hparams.get("type_vocab_size", 1))

        # convert to phantom space vocab
        def phantom(tok):
            if tok.startswith("[") and tok.endswith("]"):
                return tok
            if tok.startswith("##"):
                return tok[2:]
            return "\u2581" + tok
        tokens = list(map(phantom, tokens))

        # add vocab to gguf
        self.gguf_writer.add_tokenizer_model("bert")
        self.gguf_writer.add_tokenizer_pre(tokpre)
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_types(toktypes)

        # handle special tokens
        special_vocab = gguf.SpecialVocab(self.dir_model, n_vocab=len(tokens))
        special_vocab.add_to_gguf(self.gguf_writer)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        if name.startswith("bert."):
            name = name[5:]

        if name.endswith(".gamma"):
            name = name[:-6] + ".weight"

        if name.endswith(".beta"):
            name = name[:-5] + ".bias"

        # we are only using BERT for embeddings so we don't need the pooling layer
        if name in ("embeddings.position_ids", "pooler.dense.weight", "pooler.dense.bias"):
            return [] # we don't need these

        if name.startswith("cls.predictions"):
            return []

        if name.startswith("cls.seq_relationship"):
            return []

        if self.cls_out_labels:
            # For BertForSequenceClassification (direct projection layer)
            if name == "classifier.weight":
                name = "classifier.out_proj.weight"

            if name == "classifier.bias":
                name = "classifier.out_proj.bias"

        return [(self.map_tensor_name(name), data_torch)]

    def _xlmroberta_tokenizer_init(self) -> None:
        # we need the pad_token_id to know how to chop down position_embd matrix
        if (pad_token_id := self.hparams.get("pad_token_id")) is not None:
            self._position_offset = 1 + pad_token_id
            if "max_position_embeddings" in self.hparams:
                self.hparams["max_position_embeddings"] -= self._position_offset
        else:
            self._position_offset = None

    def _xlmroberta_set_vocab(self) -> None:
        # to avoid TypeError: Descriptors cannot be created directly
        # exception when importing sentencepiece_model_pb2
        os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
        from sentencepiece import SentencePieceProcessor
        from sentencepiece import sentencepiece_model_pb2 as model

        tokenizer_path = self.dir_model / 'sentencepiece.bpe.model'

        tokenizer_json = {}
        tokenizer_config_json = {}
        if not tokenizer_path.is_file():
            tokenizer_path = self.dir_model / 'tokenizer.json'
            tokenizer_config_path = self.dir_model / 'tokenizer_config.json'

            if not tokenizer_path.is_file():
                raise FileNotFoundError(f"File not found: {tokenizer_path}")

            from base64 import b64decode
            from transformers import AutoTokenizer
            tokenizer = AutoTokenizer.from_pretrained(self.dir_model)

            with open(tokenizer_path, "r", encoding="utf-8") as fp:
                tokenizer_json = json.load(fp)

            if tokenizer_config_path.is_file():
                with open(tokenizer_config_path, "r", encoding="utf-8") as fp:
                    tokenizer_config_json = json.load(fp)

            add_prefix = tokenizer.add_prefix_space
            remove_whitespaces = tokenizer.clean_up_tokenization_spaces
            precompiled_charsmap = b64decode(tokenizer_json["normalizer"]["precompiled_charsmap"])

            vocab_size = max(self.hparams.get("vocab_size", 0), tokenizer.vocab_size)
        else:
            sentencepiece_model = model.ModelProto()  # pyright: ignore[reportAttributeAccessIssue]
            sentencepiece_model.ParseFromString(open(tokenizer_path, "rb").read())
            assert sentencepiece_model.trainer_spec.model_type == 1  # UNIGRAM

            add_prefix = sentencepiece_model.normalizer_spec.add_dummy_prefix
            remove_whitespaces = sentencepiece_model.normalizer_spec.remove_extra_whitespaces
            precompiled_charsmap = sentencepiece_model.normalizer_spec.precompiled_charsmap

            tokenizer = SentencePieceProcessor()
            tokenizer.LoadFromFile(str(tokenizer_path))

            vocab_size = max(self.hparams.get("vocab_size", 0), tokenizer.vocab_size())

        tokens: list[bytes] = [f"[PAD{i}]".encode("utf-8") for i in range(vocab_size)]
        scores: list[float] = [-10000.0] * vocab_size
        toktypes: list[int] = [SentencePieceTokenTypes.UNUSED] * vocab_size

        if isinstance(tokenizer, SentencePieceProcessor):
            for token_id in range(tokenizer.vocab_size()):
                piece = tokenizer.IdToPiece(token_id)
                text = piece.encode("utf-8")
                score = tokenizer.GetScore(token_id)

                toktype = SentencePieceTokenTypes.NORMAL
                if tokenizer.IsUnknown(token_id):
                    toktype = SentencePieceTokenTypes.UNKNOWN
                elif tokenizer.IsControl(token_id):
                    toktype = SentencePieceTokenTypes.CONTROL
                elif tokenizer.IsUnused(token_id):
                    toktype = SentencePieceTokenTypes.UNUSED
                elif tokenizer.IsByte(token_id):
                    toktype = SentencePieceTokenTypes.BYTE

                tokens[token_id] = text
                scores[token_id] = score
                toktypes[token_id] = toktype
        else:
            added_vocab = tokenizer.get_added_vocab()
            unk_token = tokenizer_config_json.get("unk_token")
            unk_token_id = added_vocab.get(unk_token, tokenizer_json["model"].get("unk_id", 3))

            for token_id in range(tokenizer.vocab_size):
                piece = tokenizer._convert_id_to_token(token_id)
                if (piece := tokenizer._convert_id_to_token(token_id)) is not None:
                    text = piece.encode("utf-8")
                    score = tokenizer_json["model"]["vocab"][token_id][1]

                    toktype = SentencePieceTokenTypes.NORMAL
                    if token_id == unk_token_id:
                        toktype = SentencePieceTokenTypes.UNKNOWN
                    elif token_id in tokenizer.all_special_ids:
                        toktype = SentencePieceTokenTypes.CONTROL
                    elif token_id in added_vocab.values():
                        toktype = SentencePieceTokenTypes.USER_DEFINED
                    # No reliable way to detect this, but jina doesn't have any
                    # elif tokenizer.IsByte(token_id):
                    #     toktype = SentencePieceTokenTypes.BYTE

                    tokens[token_id] = text
                    scores[token_id] = score
                    toktypes[token_id] = toktype

        if isinstance(tokenizer, SentencePieceProcessor):
            # realign tokens (see HF tokenizer code)
            tokens = [b'<s>', b'<pad>', b'</s>', b'<unk>'] + tokens[3:-1]
            scores = [0.0, 0.0, 0.0, 0.0] + scores[3:-1]
            toktypes = [
                SentencePieceTokenTypes.CONTROL,
                SentencePieceTokenTypes.CONTROL,
                SentencePieceTokenTypes.CONTROL,
                SentencePieceTokenTypes.UNKNOWN,
            ] + toktypes[3:-1]

            if self.model_arch == gguf.MODEL_ARCH.NOMIC_BERT_MOE:
                # Add mask token missing from sentencepiece.bpe.model
                tokens[250001] = b'<mask>'
                scores[250001] = 0.0
                toktypes[250001] = SentencePieceTokenTypes.CONTROL

        self.gguf_writer.add_tokenizer_model("t5")
        self.gguf_writer.add_tokenizer_pre("default")
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_scores(scores)
        self.gguf_writer.add_token_types(toktypes)
        self.gguf_writer.add_add_space_prefix(add_prefix)
        self.gguf_writer.add_token_type_count(self.hparams.get("type_vocab_size", 1))
        self.gguf_writer.add_remove_extra_whitespaces(remove_whitespaces)
        if precompiled_charsmap:
            self.gguf_writer.add_precompiled_charsmap(precompiled_charsmap)

        special_vocab = gguf.SpecialVocab(self.dir_model, n_vocab=len(tokens))
        special_vocab.add_to_gguf(self.gguf_writer)


@ModelBase.register("DistilBertModel", "DistilBertForMaskedLM", "DistilBertForSequenceClassification")
class DistilBertModel(BertModel):
    model_arch = gguf.MODEL_ARCH.BERT

    def set_gguf_parameters(self):
        self.gguf_writer.add_layer_norm_eps(1e-12)
        logger.info("gguf: layer norm epsilon = 1e-12")
        super().set_gguf_parameters()

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        if name.startswith("distilbert."):
            name = name[11:]

        # These layers act as MLM head, so we don't need them
        if name.startswith("vocab_"):
            return []

        return super().modify_tensors(data_torch, name, bid)


@ModelBase.register("RobertaModel", "RobertaForSequenceClassification")
class RobertaModel(BertModel):
    model_arch = gguf.MODEL_ARCH.BERT

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # we need the pad_token_id to know how to chop down position_embd matrix
        if (pad_token_id := self.hparams.get("pad_token_id")) is not None:
            self._position_offset = 1 + pad_token_id
            if "max_position_embeddings" in self.hparams:
                self.hparams["max_position_embeddings"] -= self._position_offset
        else:
            self._position_offset = None

    def set_vocab(self):
        """Support BPE tokenizers for roberta models"""
        bpe_tok_path = self.dir_model / "tokenizer.json"
        if bpe_tok_path.exists():
            self._set_vocab_gpt2()

            # we need this to validate the size of the token_type embeddings
            # though currently we are passing all zeros to the token_type embeddings
            # "Sequence A" or "Sequence B"
            self.gguf_writer.add_token_type_count(self.hparams.get("type_vocab_size", 1))

        else:
            return super().set_vocab()

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        # if name starts with "roberta.", remove the prefix
        # e.g. https://huggingface.co/BAAI/bge-reranker-v2-m3/tree/main
        if name.startswith("roberta."):
            name = name[8:]

        # position embeddings start at pad_token_id + 1, so just chop down the weight tensor
        if name == "embeddings.position_embeddings.weight":
            if self._position_offset is not None:
                data_torch = data_torch[self._position_offset:,:]

        return super().modify_tensors(data_torch, name, bid)


@ModelBase.register("NomicBertModel")
class NomicBertModel(BertModel):
    model_arch = gguf.MODEL_ARCH.BERT

    def __init__(self, dir_model: Path, ftype: gguf.LlamaFileType, fname_out: Path, **kwargs: Any):
        hparams = kwargs.pop("hparams", None)
        if hparams is None:
            hparams = ModelBase.load_hparams(dir_model)

        self.is_moe = bool(hparams.get("moe_every_n_layers"))
        self.model_arch = gguf.MODEL_ARCH.NOMIC_BERT_MOE if self.is_moe else gguf.MODEL_ARCH.NOMIC_BERT

        super().__init__(dir_model, ftype, fname_out, hparams=hparams, **kwargs)

        self._tokenizer_is_xlmroberta = self._is_tokenizer_xlmroberta()
        if self._tokenizer_is_xlmroberta:
            self._xlmroberta_tokenizer_init()

        npos, mtp = self.hparams["n_positions"], self.hparams.get("max_trained_positions", 2048)
        if npos == 8192 and mtp == 2048:
            self.hparams["n_positions"] = 2048  # nomic-embed-text v1 and v1.5 are trained for 2048 tokens.
        elif npos == 2048 and mtp == 2048:
            self.hparams["n_positions"] = 512   # nomic-embed-text-v2-moe is trained for 512 tokens.
        else:
            raise ValueError(f"unrecognized parameters: n_positions={npos}, max_trained_positions={mtp}")

        assert self.hparams["activation_function"] == "gelu" if self.is_moe else "swiglu"

        # this doesn't do anything in the HF version
        assert self.hparams["causal"] is False
        # no bias tensors unless MoE
        assert self.hparams["qkv_proj_bias"] == self.is_moe
        assert self.hparams["mlp_fc1_bias"]  == self.is_moe
        assert self.hparams["mlp_fc2_bias"]  == self.is_moe

        # norm at end of layer
        assert self.hparams["prenorm"] is False
        # standard RoPE
        assert self.hparams["rotary_emb_fraction"] == 1.0
        assert self.hparams["rotary_emb_interleaved"] is False
        assert self.hparams["rotary_emb_scale_base"] is None

    def set_vocab(self) -> None:
        if self._tokenizer_is_xlmroberta:
            return self._xlmroberta_set_vocab()
        return super().set_vocab()

    def modify_tensors(self, data_torch: torch.Tensor, name: str, bid: int | None) -> Iterable[tuple[str, torch.Tensor]]:
        # If the tensor is an experts bias tensor, skip it by returning an empty list.
        if "mlp.experts.bias" in name:
            return []  # Explicitly return an empty list.

        if "mlp.experts.mlp.w1" in name:
            data_torch = data_torch.view(self.hparams["num_experts"], self.hparams["n_inner"], self.hparams["n_embd"])
            name += ".weight"

        if "mlp.experts.mlp.w2" in name:
            data_torch = data_torch.view(self.hparams["num_experts"], self.hparams["n_inner"], self.hparams["n_embd"])
            data_torch = data_torch.transpose(1, 2)
            name += ".weight"

        return [(self.map_tensor_name(name), data_torch)]

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_rope_freq_base(self.hparams["rotary_emb_base"])
        if self.is_moe:
            self.gguf_writer.add_moe_every_n_layers(self.hparams["moe_every_n_layers"])
            self.gguf_writer.add_expert_count(self.hparams["num_experts"])
            self.gguf_writer.add_expert_used_count(self.hparams["moe_top_k"])

    def _is_tokenizer_xlmroberta(self) -> bool:
        with open(self.dir_model / "tokenizer.json") as f:
            tokenizer_json = json.load(f)
        toktyp = tokenizer_json["model"]["type"]
        if toktyp == "Unigram":
            return True
        if toktyp == "WordPiece":
            return False
        raise ValueError(f"unknown tokenizer: {toktyp}")


@ModelBase.register("NeoBERT", "NeoBERTLMHead", "NeoBERTForSequenceClassification")
class NeoBert(BertModel):
    model_arch = gguf.MODEL_ARCH.NEO_BERT

    def set_gguf_parameters(self):
        super().set_gguf_parameters()

        # NeoBERT uses 2/3 of the intermediate size as feed forward length
        self.gguf_writer.add_feed_forward_length(int(2 * self.hparams["intermediate_size"] / 3))
        self.gguf_writer.add_rope_freq_base(10000.0)  # default value for NeoBERT
        self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.NONE)

        f_rms_eps = self.hparams.get("norm_eps", 1e-6)  # default value for NeoBERT
        self.gguf_writer.add_layer_norm_rms_eps(f_rms_eps)
        logger.info(f"gguf: rms norm epsilon = {f_rms_eps}")

        self.gguf_writer.add_pooling_type(gguf.PoolingType.CLS) # https://huggingface.co/chandar-lab/NeoBERT#how-to-use

    def modify_tensors(self, data_torch, name, bid):
        if name.startswith("decoder."):
            return []

        if name.startswith("model."):
            name = name[6:]

        return super().modify_tensors(data_torch, name, bid)


@ModelBase.register("XLMRobertaModel", "XLMRobertaForSequenceClassification")
class XLMRobertaModel(BertModel):
    model_arch = gguf.MODEL_ARCH.BERT

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._xlmroberta_tokenizer_init()

    def set_vocab(self):
        self._xlmroberta_set_vocab()

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        # if name starts with "roberta.", remove the prefix
        # e.g. https://huggingface.co/BAAI/bge-reranker-v2-m3/tree/main
        if name.startswith("roberta."):
            name = name[8:]

        # position embeddings start at pad_token_id + 1, so just chop down the weight tensor
        if name == "embeddings.position_embeddings.weight":
            if self._position_offset is not None:
                data_torch = data_torch[self._position_offset:,:]

        return super().modify_tensors(data_torch, name, bid)


@ModelBase.register("GemmaForCausalLM")
class GemmaModel(TextModel):
    model_arch = gguf.MODEL_ARCH.GEMMA

    def set_vocab(self):
        self._set_vocab_sentencepiece()

        # TODO: these special tokens should be exported only for the CodeGemma family
        special_vocab = gguf.SpecialVocab(self.dir_model, load_merges=False,
                                          special_token_types = ['prefix', 'suffix', 'middle', 'fsep', 'eot'])
        special_vocab._set_special_token("prefix", 67)
        special_vocab._set_special_token("suffix", 69)
        special_vocab._set_special_token("middle", 68)
        special_vocab._set_special_token("fsep",   70)
        special_vocab._set_special_token("eot",    107)
        special_vocab.chat_template = None  # do not add it twice
        special_vocab.add_to_gguf(self.gguf_writer)

        self.gguf_writer.add_add_space_prefix(False)

    def set_gguf_parameters(self):
        hparams = self.hparams
        block_count = hparams["num_hidden_layers"]

        self.gguf_writer.add_context_length(hparams["max_position_embeddings"])
        self.gguf_writer.add_embedding_length(hparams["hidden_size"])
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_feed_forward_length(hparams["intermediate_size"])
        self.gguf_writer.add_head_count(hparams["num_attention_heads"])
        self.gguf_writer.add_head_count_kv(self.hparams["num_key_value_heads"] if "num_key_value_heads" in hparams else hparams["num_attention_heads"])
        self.gguf_writer.add_layer_norm_rms_eps(self.hparams["rms_norm_eps"])
        self.gguf_writer.add_key_length(hparams["head_dim"])
        self.gguf_writer.add_value_length(hparams["head_dim"])
        self.gguf_writer.add_file_type(self.ftype)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        # lm_head is not used in llama.cpp, while autoawq will include this tensor in model
        # To prevent errors, skip loading lm_head.weight.
        if name == "lm_head.weight":
            logger.debug(f"Skipping get tensor {name!r} in safetensors so that convert can end normally.")
            return []

        # ref: https://github.com/huggingface/transformers/blob/fc37f38915372c15992b540dfcbbe00a916d4fc6/src/transformers/models/gemma/modeling_gemma.py#L89
        if name.endswith("norm.weight"):
            data_torch = data_torch + 1

        return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("Gemma2ForCausalLM")
class Gemma2Model(TextModel):
    model_arch = gguf.MODEL_ARCH.GEMMA2

    def set_vocab(self):
        self._set_vocab_sentencepiece()

        self.gguf_writer.add_add_space_prefix(False)

    def set_gguf_parameters(self):
        hparams = self.hparams
        block_count = hparams["num_hidden_layers"]

        self.gguf_writer.add_context_length(hparams["max_position_embeddings"])
        self.gguf_writer.add_embedding_length(hparams["hidden_size"])
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_feed_forward_length(hparams["intermediate_size"])
        self.gguf_writer.add_head_count(hparams["num_attention_heads"])
        self.gguf_writer.add_head_count_kv(self.hparams["num_key_value_heads"] if "num_key_value_heads" in hparams else hparams["num_attention_heads"])
        self.gguf_writer.add_layer_norm_rms_eps(self.hparams["rms_norm_eps"])
        self.gguf_writer.add_key_length(hparams["head_dim"])
        self.gguf_writer.add_value_length(hparams["head_dim"])
        self.gguf_writer.add_file_type(self.ftype)
        self.gguf_writer.add_attn_logit_softcapping(
            self.hparams["attn_logit_softcapping"]
        )
        self.gguf_writer.add_final_logit_softcapping(
            self.hparams["final_logit_softcapping"]
        )
        self.gguf_writer.add_sliding_window(self.hparams["sliding_window"])

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        # lm_head is not used in llama.cpp, while autoawq will include this tensor in model
        # To prevent errors, skip loading lm_head.weight.
        if name == "lm_head.weight":
            logger.debug(f"Skipping get tensor {name!r} in safetensors so that convert can end normally.")
            return []

        # ref: https://github.com/huggingface/transformers/blob/fc37f38915372c15992b540dfcbbe00a916d4fc6/src/transformers/models/gemma/modeling_gemma.py#L89
        if name.endswith("norm.weight"):
            data_torch = data_torch + 1

        return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("Gemma3ForCausalLM", "Gemma3ForConditionalGeneration")
class Gemma3Model(TextModel):
    model_arch = gguf.MODEL_ARCH.GEMMA3
    norm_shift = 1.0  # Gemma3RMSNorm adds 1.0 to the norm value

    def set_vocab(self):
        self._set_vocab_sentencepiece()

        self.gguf_writer.add_add_space_prefix(False)

    def set_gguf_parameters(self):
        hparams = self.hparams
        block_count = hparams["num_hidden_layers"]

        # some default values are not specified in the hparams
        self.gguf_writer.add_context_length(hparams.get("max_position_embeddings", 131072))
        self.gguf_writer.add_embedding_length(hparams["hidden_size"])
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_feed_forward_length(hparams["intermediate_size"])
        self.gguf_writer.add_head_count(hparams.get("num_attention_heads", 8))
        self.gguf_writer.add_layer_norm_rms_eps(self.hparams.get("rms_norm_eps", 1e-6))
        self.gguf_writer.add_key_length(hparams.get("head_dim", 256))
        self.gguf_writer.add_value_length(hparams.get("head_dim", 256))
        self.gguf_writer.add_file_type(self.ftype)
        self.gguf_writer.add_rope_freq_base(hparams.get("rope_theta", 1_000_000.0)) # for global layers
        # attn_logit_softcapping is removed in Gemma3
        assert hparams.get("attn_logit_softcapping") is None
        self.gguf_writer.add_sliding_window(hparams["sliding_window"])
        self.gguf_writer.add_head_count_kv(hparams.get("num_key_value_heads", 4))
        if hparams.get("rope_scaling") is not None:
            assert hparams["rope_scaling"]["rope_type"] == "linear"
            # important: this rope_scaling is only applied for global layers, and not used by 1B model
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.LINEAR)
            self.gguf_writer.add_rope_scaling_factor(hparams["rope_scaling"]["factor"])

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        if "language_model." in name:
            name = name.replace("language_model.", "")

        elif name.startswith("multi_modal_projector.") or name.startswith("vision_tower.") \
                or name.startswith("multimodal_projector.") or name.startswith("vision_model."):
            return [] # skip vision tensors

        # remove OOV (out-of-vocabulary) rows in token_embd
        if "embed_tokens.weight" in name:
            vocab = self._create_vocab_sentencepiece()
            tokens = vocab[0]
            data_torch = data_torch[:len(tokens)]

        # ref code in Gemma3RMSNorm
        # output = output * (1.0 + self.weight.float())
        # note: this is not the case on gemma3n
        if name.endswith("norm.weight"):
            data_torch = data_torch + self.norm_shift

        return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("Gemma3ForConditionalGeneration")
class Gemma3VisionModel(MmprojModel):
    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        hparams = self.hparams
        self.gguf_writer.add_clip_projector_type(gguf.VisionProjectorType.GEMMA3)
        # default values below are taken from HF tranformers code
        self.gguf_writer.add_vision_attention_layernorm_eps(hparams.get("layer_norm_eps", 1e-6))
        self.gguf_writer.add_vision_use_gelu(True)
        # calculate proj_scale_factor (used by tinygemma3 test model)
        image_seq_length = self.preprocessor_config.get("image_seq_length", 256)
        n_per_side = int(image_seq_length ** 0.5)
        image_size = self.hparams["image_size"]
        patch_size = self.hparams["patch_size"]
        proj_scale_factor = (image_size // patch_size) // n_per_side
        if proj_scale_factor > 0 and proj_scale_factor != 4:
            # we only need to write this if it's not the default value
            # in this case, we are converting a test model
            self.gguf_writer.add_vision_projector_scale_factor(proj_scale_factor)

    def tensor_force_quant(self, name, new_name, bid, n_dims):
        del bid, new_name, n_dims  # unused
        # related to https://github.com/ggml-org/llama.cpp/issues/13025
        if "input_projection" in name:
            return gguf.GGMLQuantizationType.F16
        if ".embeddings." in name:
            return gguf.GGMLQuantizationType.F32
        return False

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        if "vision_model.head." in name:
            return [] # skip redundant tensors for tinygemma3

        if name.startswith("multi_modal_projector.") or name.startswith("vision_tower.") \
                or name.startswith("multimodal_projector.") or name.startswith("vision_model."):
            # process vision tensors
            name = name.replace("_weight", ".weight")

            # correct norm value ; only this "soft_emb_norm" need to be corrected as it's part of Gemma projector
            # the other norm values are part of SigLIP model, and they are already correct
            # ref code: Gemma3RMSNorm
            if "soft_emb_norm.weight" in name:
                logger.info(f"Correcting norm value for '{name}'")
                data_torch = data_torch + 1

            return [(self.map_tensor_name(name), data_torch)]

        return [] # skip other tensors


@ModelBase.register("Gemma3nForConditionalGeneration")
class Gemma3NModel(Gemma3Model):
    model_arch = gguf.MODEL_ARCH.GEMMA3N
    norm_shift = 0.0 # same value with Gemma3p5RMSNorm scale_shift on python code

    _altup_proj: list[Tensor] = []
    _altup_unembd: list[Tensor] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        assert self.hparams["altup_num_inputs"] == 4, "Current conversion only supports 4 altup inputs"
        self._altup_proj = [
            torch.Tensor(), # to be replaced
            torch.Tensor(), # to be replaced
            torch.Tensor(), # to be replaced
        ]
        self._altup_unembd = [
            torch.Tensor(), # to be replaced
            torch.Tensor(), # to be replaced
            torch.Tensor(), # to be replaced
        ]

    def set_vocab(self):
        super().set_vocab()

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_altup_active_idx(self.hparams["altup_active_idx"])
        self.gguf_writer.add_altup_num_inputs(self.hparams["altup_num_inputs"])
        self.gguf_writer.add_embedding_length_per_layer_input(self.hparams["hidden_size_per_layer_input"])
        self.gguf_writer.add_shared_kv_layers(self.hparams["num_kv_shared_layers"])

        activation_sparsity_scale = []
        for s in self.hparams["activation_sparsity_pattern"]:
            normal_dist = torch.distributions.normal.Normal(0, 1)
            std_multiplier = normal_dist.icdf(torch.tensor(s, dtype=torch.float32))
            activation_sparsity_scale.append(std_multiplier.item())
        self.gguf_writer.add_activation_sparsity_scale(activation_sparsity_scale)

        sliding_window_pattern = []
        for t in self.hparams["layer_types"]:
            sliding_window_pattern.append(t == "sliding_attention")
        self.gguf_writer.add_sliding_window_pattern(sliding_window_pattern)

    def _stack_matrices(self, matrices: list[Tensor]) -> Tensor | None:
        has_all = all(m.numel() > 0 for m in matrices)
        if not has_all:
            return None
        else:
            return torch.stack(matrices, dim=0)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        if name.endswith("_scale"):
            name = name + ".weight"

        # TODO: implement self.prediction_coefs.weight.clamp_(...)

        if "language_model." not in name:
            return [] # skip non-language model tensors

        if "altup_unembed_projections" in name:
            data_torch = data_torch.to(device="cpu")
            if ".0." in name:
                self._altup_unembd[0] = data_torch
            elif ".1." in name:
                self._altup_unembd[1] = data_torch
            elif ".2." in name:
                self._altup_unembd[2] = data_torch
            else:
                raise ValueError(f"Unknown name: {name}")
            out = self._stack_matrices(self._altup_unembd)
            if out is not None:
                return [(self.map_tensor_name("model.altup_unembed_projections.weight"), out)]
            else:
                return []

        if "altup_projections" in name:
            data_torch = data_torch.to(device="cpu")
            if ".0." in name:
                self._altup_proj[0] = data_torch
            elif ".1." in name:
                self._altup_proj[1] = data_torch
            elif ".2." in name:
                self._altup_proj[2] = data_torch
            else:
                raise ValueError(f"Unknown name: {name}")
            out = self._stack_matrices(self._altup_proj)
            if out is not None:
                return [(self.map_tensor_name("model.altup_projections.weight"), out)]
            else:
                return []

        return super().modify_tensors(data_torch, name, bid)


@ModelBase.register("Starcoder2ForCausalLM")
class StarCoder2Model(TextModel):
    model_arch = gguf.MODEL_ARCH.STARCODER2


@ModelBase.register("Rwkv6ForCausalLM")
class Rwkv6Model(TextModel):
    model_arch = gguf.MODEL_ARCH.RWKV6

    def set_vocab(self):
        self._set_vocab_rwkv_world()

    def set_gguf_parameters(self):
        block_count = self.hparams["num_hidden_layers"]
        head_size = self.hparams["head_size"]
        hidden_size = self.hparams["hidden_size"]
        layer_norm_eps = self.hparams["layer_norm_epsilon"]
        rescale_every_n_layers = self.hparams["rescale_every"]
        intermediate_size = self.hparams["intermediate_size"] if self.hparams["intermediate_size"] is not None else int((hidden_size * 3.5) // 32 * 32)
        time_mix_extra_dim = 64 if hidden_size == 4096 else 32
        time_decay_extra_dim = 128 if hidden_size == 4096 else 64

        # RWKV isn't context limited
        self.gguf_writer.add_context_length(1048576)
        self.gguf_writer.add_embedding_length(hidden_size)
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_layer_norm_eps(layer_norm_eps)
        self.gguf_writer.add_rescale_every_n_layers(rescale_every_n_layers)
        self.gguf_writer.add_wkv_head_size(head_size)
        self.gguf_writer.add_time_mix_extra_dim(time_mix_extra_dim)
        self.gguf_writer.add_time_decay_extra_dim(time_decay_extra_dim)
        self.gguf_writer.add_feed_forward_length(intermediate_size)
        self.gguf_writer.add_file_type(self.ftype)

        # required by llama.cpp, unused
        self.gguf_writer.add_head_count(0)

    lerp_weights: dict[int, dict[str, Tensor]] = {}

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        new_name = self.map_tensor_name(name)

        if not (new_name.endswith(".weight") or new_name.endswith(".bias")):
            new_name += ".weight"

        if new_name.endswith("time_mix_w1.weight") or new_name.endswith("time_mix_decay_w1.weight") or new_name.endswith("time_mix_decay_w2.weight"):
            data_torch = data_torch.transpose(0, 1)

        if new_name.endswith("time_mix_w2.weight"):
            data_torch = data_torch.permute(0, 2, 1)

        if new_name.endswith("time_mix_decay.weight") or "lerp" in new_name:
            data_torch = data_torch.squeeze()

        try:
            rescale_every_n_layers = self.hparams["rescale_every"]
            if rescale_every_n_layers > 0:
                if new_name.endswith("time_mix_output.weight") or new_name.endswith("channel_mix_value.weight"):
                    data_torch = data_torch.div_(2 ** int(bid // rescale_every_n_layers))
        except KeyError:
            pass

        # concat time_mix_lerp weights to reduce some cpu overhead
        # also reduces the number of tensors in the model
        if bid is not None and "time_mix_lerp" in new_name and "time_mix_lerp_x" not in new_name:
            try:
                self.lerp_weights[bid][new_name] = data_torch
            except KeyError:
                self.lerp_weights[bid] = {new_name: data_torch}
            if all(f"blk.{bid}.time_mix_lerp_{i}.weight" in self.lerp_weights[bid].keys() for i in ["w", "k", "v", "r", "g"]):
                new_name = f"blk.{bid}.time_mix_lerp_fused.weight"
                data = torch.stack([self.lerp_weights[bid][f"blk.{bid}.time_mix_lerp_{i}.weight"].unsqueeze(0) for i in ["w", "k", "v", "r", "g"]], dim=0).unsqueeze(1)
                yield (new_name, data)
            return

        yield (new_name, data_torch)


@ModelBase.register("RWKV6Qwen2ForCausalLM")
class RWKV6Qwen2Model(Rwkv6Model):
    model_arch = gguf.MODEL_ARCH.RWKV6QWEN2

    def set_vocab(self):
        try:
            self._set_vocab_sentencepiece()
        except FileNotFoundError:
            self._set_vocab_gpt2()

    def set_gguf_parameters(self):
        block_count = self.hparams["num_hidden_layers"]
        num_attention_heads = self.hparams["num_attention_heads"]
        num_key_value_heads = self.hparams["num_key_value_heads"]
        hidden_size = self.hparams["hidden_size"]
        head_size = hidden_size // num_attention_heads
        rms_norm_eps = self.hparams["rms_norm_eps"]
        intermediate_size = self.hparams["intermediate_size"]
        time_mix_extra_dim = self.hparams.get("lora_rank_tokenshift", 64 if hidden_size >= 4096 else 32)
        time_decay_extra_dim = self.hparams.get("lora_rank_decay", 128 if hidden_size >= 4096 else 64)

        # RWKV isn't context limited
        self.gguf_writer.add_context_length(1048576)
        self.gguf_writer.add_embedding_length(hidden_size)
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_wkv_head_size(head_size)
        self.gguf_writer.add_time_mix_extra_dim(time_mix_extra_dim)
        self.gguf_writer.add_time_decay_extra_dim(time_decay_extra_dim)
        self.gguf_writer.add_feed_forward_length(intermediate_size)
        self.gguf_writer.add_file_type(self.ftype)

        # special parameters for time_mixing in RWKV6QWEN2
        self.gguf_writer.add_layer_norm_rms_eps(rms_norm_eps)
        self.gguf_writer.add_token_shift_count(1)
        # RWKV6QWEN2 use grouped key/value like GQA
        self.gguf_writer.add_head_count_kv(num_key_value_heads)

        # required by llama.cpp, unused
        self.gguf_writer.add_head_count(0)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        for new_name, data in super().modify_tensors(data_torch, name, bid):
            if "time_mix_w1" in new_name or "time_mix_w2" in new_name:
                data = data.view(5, -1, data.shape[-1])
                # rwkv6qwen2 has a different order of rkvwg instead of the original wkvrg
                # permute them here to avoid code changes
                data = torch.stack([data[3], data[1], data[2], data[0], data[4]], dim=0).view(-1, data.shape[-1])
                if "w2" in new_name:
                    data = data.view(5, -1, data.shape[-1])
                yield (new_name, data)
                continue
            yield (new_name, data)


@ModelBase.register("Rwkv7ForCausalLM", "RWKV7ForCausalLM")
class Rwkv7Model(TextModel):
    model_arch = gguf.MODEL_ARCH.RWKV7

    def set_vocab(self):
        self._set_vocab_rwkv_world()

    def calc_lora_rank(self, hidden_size, exponent, multiplier):
        return max(1, round(hidden_size ** exponent * multiplier / 32)) * 32

    def set_gguf_parameters(self):
        block_count = self.hparams["num_hidden_layers"]
        try:
            head_size = self.hparams["head_size"]
            layer_norm_eps = self.hparams["layer_norm_epsilon"]
        except KeyError:
            head_size = self.hparams["head_dim"]
            layer_norm_eps = self.hparams["norm_eps"]
        hidden_size = self.hparams["hidden_size"]
        intermediate_size = self.hparams["intermediate_size"] if self.hparams["intermediate_size"] is not None else (hidden_size * 4)

        # ICLR: In-Context-Learning-Rate
        try:
            lora_rank_decay = self.hparams["lora_rank_decay"] if self.hparams["lora_rank_decay"] is not None else self.calc_lora_rank(hidden_size, 0.5, 1.8)
            lora_rank_iclr = self.hparams["lora_rank_iclr"] if self.hparams["lora_rank_iclr"] is not None else self.calc_lora_rank(hidden_size, 0.5, 1.8)
            lora_rank_value_residual_mix = self.hparams["lora_rank_value_residual_mix"] if self.hparams["lora_rank_value_residual_mix"] is not None else self.calc_lora_rank(hidden_size, 0.5, 1.3)
            lora_rank_gate = self.hparams["lora_rank_gate"] if self.hparams["lora_rank_gate"] is not None else self.calc_lora_rank(hidden_size, 0.8, 0.6)
        except KeyError:
            lora_rank_decay = self.hparams["decay_low_rank_dim"] if self.hparams["decay_low_rank_dim"] is not None else self.calc_lora_rank(hidden_size, 0.5, 1.8)
            lora_rank_iclr = self.hparams["a_low_rank_dim"] if self.hparams["a_low_rank_dim"] is not None else self.calc_lora_rank(hidden_size, 0.5, 1.8)
            lora_rank_value_residual_mix = self.hparams["v_low_rank_dim"] if self.hparams["v_low_rank_dim"] is not None else self.calc_lora_rank(hidden_size, 0.5, 1.3)
            lora_rank_gate = self.hparams["gate_low_rank_dim"] if self.hparams["gate_low_rank_dim"] is not None else self.calc_lora_rank(hidden_size, 0.8, 0.6)

        # RWKV isn't context limited
        self.gguf_writer.add_context_length(1048576)
        self.gguf_writer.add_embedding_length(hidden_size)
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_layer_norm_eps(layer_norm_eps)
        self.gguf_writer.add_wkv_head_size(head_size)
        self.gguf_writer.add_decay_lora_rank(lora_rank_decay)
        self.gguf_writer.add_iclr_lora_rank(lora_rank_iclr)
        self.gguf_writer.add_value_residual_mix_lora_rank(lora_rank_value_residual_mix)
        self.gguf_writer.add_gate_lora_rank(lora_rank_gate)
        self.gguf_writer.add_feed_forward_length(intermediate_size)
        self.gguf_writer.add_file_type(self.ftype)

        # required by llama.cpp, unused
        self.gguf_writer.add_head_count(0)

    lerp_weights: dict[int, dict[str, Tensor]] = {}
    lora_needs_transpose: bool = True

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        # unify tensor names here to make life easier
        name = name.replace("blocks", "layers").replace("ffn", "feed_forward")
        name = name.replace("self_attn", "attention").replace("attn", "attention")
        name = name.replace("time_mixer.", "")
        # lora layer names in fla-hub's impl
        if "_lora.lora" in name:
            self.lora_needs_transpose = False
        name = name.replace("_lora.lora.0.weight", "1.weight")
        name = name.replace("_lora.lora.2.weight", "2.weight")
        name = name.replace("_lora.lora.2.bias", "0.weight")

        name = name.replace("feed_forward_norm", "ln2")
        name = name.replace("g_norm", "ln_x")

        if "attention.v" in name and "value" not in self.map_tensor_name(name) and bid == 0:
            # some models have dummy v0/v1/v2 on first layer while others don't
            # ignore them all since they are not used
            return

        wkv_has_gate = self.hparams.get("wkv_has_gate", True)
        lerp_list = ["r", "w", "k", "v", "a", "g"] if wkv_has_gate else ["r", "w", "k", "v", "a"]

        if bid is not None and "attention.x_" in name:
            if "attention.x_x" in name:
                # already concatenated
                new_name = f"blk.{bid}.time_mix_lerp_fused.weight"
                data = data_torch.reshape(len(lerp_list), 1, 1, -1)
                yield (new_name, data)
            else:
                try:
                    self.lerp_weights[bid][name] = data_torch
                except KeyError:
                    self.lerp_weights[bid] = {name: data_torch}
                if all(f"model.layers.{bid}.attention.x_{i}" in self.lerp_weights[bid].keys() for i in lerp_list):
                    new_name = f"blk.{bid}.time_mix_lerp_fused.weight"
                    data = torch.stack([self.lerp_weights[bid][f"model.layers.{bid}.attention.x_{i}"] for i in lerp_list], dim=0)
                    yield (new_name, data)
            return
        else:
            data_torch = data_torch.squeeze()
            new_name = self.map_tensor_name(name)

            if not (new_name.endswith(".weight") or new_name.endswith(".bias")):
                new_name += ".weight"

            if self.lora_needs_transpose and any(
                new_name.endswith(t) for t in [
                    "time_mix_w1.weight", "time_mix_w2.weight",
                    "time_mix_a1.weight", "time_mix_a2.weight",
                    "time_mix_v1.weight", "time_mix_v2.weight",
                    "time_mix_g1.weight", "time_mix_g2.weight",
                ]
            ):
                data_torch = data_torch.transpose(0, 1)

            if 'r_k' in new_name:
                data_torch = data_torch.flatten()

            if bid == 0 and "time_mix_a" in new_name:
                # dummy v0/v1/v2 on first layer
                # easist way to make llama happy
                yield (new_name.replace("time_mix_a", "time_mix_v"), data_torch)

            yield (new_name, data_torch)


@ModelBase.register("RwkvHybridForCausalLM")
class ARwkv7Model(Rwkv7Model):
    model_arch = gguf.MODEL_ARCH.ARWKV7

    def set_vocab(self):
        try:
            self._set_vocab_sentencepiece()
        except FileNotFoundError:
            self._set_vocab_gpt2()

    def set_gguf_parameters(self):
        block_count = self.hparams["num_hidden_layers"]
        hidden_size = self.hparams["hidden_size"]
        head_size = self.hparams["head_size"]
        rms_norm_eps = self.hparams["rms_norm_eps"]
        intermediate_size = self.hparams["intermediate_size"]
        wkv_has_gate = self.hparams["wkv_has_gate"]
        assert self.hparams["wkv_version"] == 7

        # ICLR: In-Context-Learning-Rate
        lora_rank_decay = 64
        lora_rank_iclr = 64
        lora_rank_value_residual_mix = 32
        lora_rank_gate = 128 if wkv_has_gate else 0

        # RWKV isn't context limited
        self.gguf_writer.add_context_length(1048576)
        self.gguf_writer.add_embedding_length(hidden_size)
        self.gguf_writer.add_block_count(block_count)
        self.gguf_writer.add_layer_norm_rms_eps(rms_norm_eps)
        self.gguf_writer.add_wkv_head_size(head_size)
        self.gguf_writer.add_decay_lora_rank(lora_rank_decay)
        self.gguf_writer.add_iclr_lora_rank(lora_rank_iclr)
        self.gguf_writer.add_value_residual_mix_lora_rank(lora_rank_value_residual_mix)
        self.gguf_writer.add_gate_lora_rank(lora_rank_gate)
        self.gguf_writer.add_feed_forward_length(intermediate_size)
        self.gguf_writer.add_file_type(self.ftype)
        self.gguf_writer.add_token_shift_count(1)

        # required by llama.cpp, unused
        self.gguf_writer.add_head_count(0)


@ModelBase.register("MambaForCausalLM", "MambaLMHeadModel", "FalconMambaForCausalLM")
class MambaModel(TextModel):
    model_arch = gguf.MODEL_ARCH.MAMBA

    def __init__(self, dir_model: Path, *args, **kwargs):
        # Avoid using AutoConfig for hparams
        hparams = kwargs.pop("hparams", None)
        if hparams is None:
            with open(dir_model / "config.json", "r", encoding="utf-8") as f:
                hparams = json.load(f)
        super().__init__(dir_model, *args, hparams=hparams, **kwargs)

    def set_vocab(self):
        vocab_size = self.hparams["vocab_size"]
        # Round vocab size to next multiple of 8
        pad_vocab = self.hparams.get("pad_vocab_size_multiple", 8)
        # pad using ceiling division
        # ref: https://stackoverflow.com/a/17511341/22827863
        vocab_size = -(vocab_size // -pad_vocab) * pad_vocab
        self.hparams["vocab_size"] = vocab_size

        if (self.dir_model / "tokenizer.json").is_file():
            self._set_vocab_gpt2()
        elif (self.dir_model / "tokenizer.model").is_file():
            self._set_vocab_sentencepiece()
        else:
            # Use the GPT-NeoX tokenizer when no tokenizer files are present
            self._set_vocab_builtin("gpt-neox", vocab_size)

    def set_gguf_parameters(self):
        d_model = self.find_hparam(["hidden_size",       "d_model"])
        d_conv  = self.find_hparam(["conv_kernel",       "d_conv"],  optional=True) or 4
        d_inner = self.find_hparam(["intermediate_size", "d_inner"], optional=True) or 2 * d_model
        d_state = self.find_hparam(["state_size",        "d_state"], optional=True) or 16
        # ceiling division
        # ref: https://stackoverflow.com/a/17511341/22827863
        # ref: https://github.com/state-spaces/mamba/blob/ce59daea3a090d011d6476c6e5b97f6d58ddad8b/mamba_ssm/modules/mamba_simple.py#L58
        dt_rank      = self.find_hparam(["time_step_rank",     "dt_rank"],      optional=True) or -(d_model // -16)
        rms_norm_eps = self.find_hparam(["layer_norm_epsilon", "rms_norm_eps"], optional=True) or 1e-5
        use_dt_b_c_norm = False
        # For falconmamba we do apply RMS norm on B / DT and C layers
        if self.find_hparam(["model_type"], optional=True) in ("falcon_mamba",):
            use_dt_b_c_norm = True
        # Fail early for models which don't have a block expansion factor of 2
        assert d_inner == 2 * d_model

        self.gguf_writer.add_context_length(2**20) # arbitrary value; for those who use the default
        self.gguf_writer.add_embedding_length(d_model)
        self.gguf_writer.add_feed_forward_length(0) # unused, but seemingly required when loading
        self.gguf_writer.add_head_count(0) # unused, but seemingly required when loading
        self.gguf_writer.add_block_count(self.block_count)
        self.gguf_writer.add_ssm_conv_kernel(d_conv)
        self.gguf_writer.add_ssm_inner_size(d_inner)
        self.gguf_writer.add_ssm_state_size(d_state)
        self.gguf_writer.add_ssm_time_step_rank(dt_rank)
        self.gguf_writer.add_layer_norm_rms_eps(rms_norm_eps)
        self.gguf_writer.add_ssm_dt_b_c_rms(use_dt_b_c_norm) # For classic Mamba we don't apply rms norm on B / DT layers
        self.gguf_writer.add_file_type(self.ftype)

    _tok_embd = None

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        output_name = self.format_tensor_name(gguf.MODEL_TENSOR.OUTPUT)
        tok_embd_name = self.format_tensor_name(gguf.MODEL_TENSOR.TOKEN_EMBD)

        new_name = self.map_tensor_name(name)

        if name.endswith(".A_log"):
            logger.debug("A_log --> A ==> " + new_name)
            data_torch = -torch.exp(data_torch)

        # [4 1 8192 1] -> [4 8192 1 1]
        if self.match_model_tensor_name(new_name, gguf.MODEL_TENSOR.SSM_CONV1D, bid):
            data_torch = data_torch.squeeze()

        # assuming token_embd.weight is seen before output.weight
        if self._tok_embd is not None and new_name == output_name:
            if torch.equal(self._tok_embd, data_torch):
                logger.debug(f"{output_name} is equivalent to {tok_embd_name}, omitting")
                return []
        elif new_name == tok_embd_name:
            self._tok_embd = data_torch

        return [(new_name, data_torch)]


@ModelBase.register("Mamba2ForCausalLM")
class Mamba2Model(TextModel):
    model_arch = gguf.MODEL_ARCH.MAMBA2

    def __init__(self, dir_model: Path, *args, **kwargs):
        # Avoid using AutoConfig for hparams
        # It wrongly assumes all Mamba2 models are Mamba-Codestral-7B-v0.1
        hparams = kwargs.pop("hparams", None)
        if hparams is None:
            with open(dir_model / "config.json", "r", encoding="utf-8") as f:
                hparams = json.load(f)
        super().__init__(dir_model, *args, hparams=hparams, **kwargs)
        self.d_model = self.find_hparam(["hidden_size", "d_model", "dim"])
        self.d_inner = self.find_hparam(["mamba_d_ssm", "intermediate_size", "d_inner"], optional=True) or 2 * self.d_model
        self.n_group = self.find_hparam(["n_groups"], optional=True) or 1

    def set_vocab(self):
        vocab_size = self.hparams["vocab_size"]
        # Round vocab size to next multiple of 16
        pad_vocab = self.hparams.get("pad_vocab_size_multiple", 16)
        # pad using ceiling division
        # ref: https://stackoverflow.com/a/17511341/22827863
        vocab_size = -(vocab_size // -pad_vocab) * pad_vocab
        self.hparams["vocab_size"] = vocab_size

        if (self.dir_model / "tokenizer.model").is_file():
            self._set_vocab_sentencepiece()
        elif (self.dir_model / "tokenizer.model.v3").is_file():
            # mamba-codestral
            raise NotImplementedError(f"Please rename {self.dir_model / 'tokenizer.model.v3'} to {self.dir_model / 'tokenizer.model'}")
        elif (self.dir_model / "tokenizer.json").is_file():
            self._set_vocab_gpt2()
        else:
            # Use the GPT-NeoX tokenizer when no tokenizer files are present
            self._set_vocab_builtin("gpt-neox", vocab_size)

    def set_gguf_parameters(self):
        d_conv  = self.find_hparam(["conv_kernel", "d_conv"],     optional=True) or 4
        d_state = self.find_hparam(["state_size",  "d_state"],    optional=True) or 128
        head_dim = self.find_hparam(["mamba_d_head", "head_dim"], optional=True) or 64

        rms_norm_eps = self.find_hparam(["layer_norm_epsilon", "rms_norm_eps"], optional=True) or 1e-5

        # Fail early for models which don't have a block expansion factor of 2
        # TODO: does this really matter?
        # skip the assertion for FalconH1 Model
        if self.model_arch != gguf.MODEL_ARCH.FALCON_H1:
            assert self.d_inner == 2 * self.d_model
            assert self.d_inner % head_dim == 0

        self.gguf_writer.add_context_length(2**20)  # arbitrary value; for those who use the default
        self.gguf_writer.add_embedding_length(self.d_model)
        self.gguf_writer.add_feed_forward_length(0)  # unused, but seemingly required when loading
        self.gguf_writer.add_head_count(0)  # unused, but seemingly required when loading
        self.gguf_writer.add_block_count(self.block_count)
        self.gguf_writer.add_ssm_conv_kernel(d_conv)
        self.gguf_writer.add_ssm_inner_size(self.d_inner)
        self.gguf_writer.add_ssm_state_size(d_state)
        self.gguf_writer.add_ssm_time_step_rank(self.d_inner // head_dim)
        self.gguf_writer.add_ssm_group_count(self.n_group)
        self.gguf_writer.add_layer_norm_rms_eps(rms_norm_eps)
        self.gguf_writer.add_file_type(self.ftype)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:

        if name.startswith("model.backbone") or name.startswith("model.lm_head"):
            # map Mamba-Codestral-7B-v0.1 tensor names to the names used by Mamba-2
            name = name.removeprefix("model.")

        if name.endswith(".dt_bias"):
            name = name.rpartition(".dt_bias")[0] + ".dt_proj.bias"

        new_name = self.map_tensor_name(name)

        if self.match_model_tensor_name(new_name, gguf.MODEL_TENSOR.SSM_CONV1D, bid):
            data_torch = data_torch.squeeze()
        elif any(self.match_model_tensor_name(new_name, t, bid, suffix="") for t in [
            gguf.MODEL_TENSOR.SSM_A,
            gguf.MODEL_TENSOR.SSM_D,
        ]):
            # unsqueeze A to use similar shape semantics as Mamba-1
            # (D is also unsqueezed, but for more straightforward broadcast internally)
            data_torch = data_torch.reshape((*data_torch.shape, 1))
        elif self.match_model_tensor_name(new_name, gguf.MODEL_TENSOR.SSM_NORM, bid):
            data_torch = data_torch.reshape((self.n_group, self.d_inner // self.n_group))

        if name.endswith(".A_log"):
            logger.debug("A_log --> A ==> " + new_name)
            data_torch = -torch.exp(data_torch)

        yield (new_name, data_torch)


@ModelBase.register("JambaForCausalLM")
class JambaModel(TextModel):
    model_arch = gguf.MODEL_ARCH.JAMBA

    def get_vocab_base_pre(self, tokenizer) -> str:
        del tokenizer  # unused

        return "gpt-2"

    def set_vocab(self):
        if (self.dir_model / "tokenizer.model").is_file():
            # Using Jamba's tokenizer.json causes errors on model load
            # (something about "byte not found in vocab"),
            # but there's a working tokenizer.model
            self._set_vocab_sentencepiece()
        else:
            # Some Jamba models only have a tokenizer.json, which works.
            self._set_vocab_gpt2()

    def set_gguf_parameters(self):
        d_model = self.find_hparam(["hidden_size", "mamba_d_model"])
        d_conv  = self.find_hparam(["mamba_d_conv"],  optional=True) or 4
        d_inner = self.hparams["mamba_expand"] * d_model
        d_state = self.find_hparam(["mamba_d_state"], optional=True) or 16
        # ceiling division
        # ref: https://stackoverflow.com/a/17511341/22827863
        # ref: https://github.com/state-spaces/mamba/blob/ce59daea3a090d011d6476c6e5b97f6d58ddad8b/mamba_ssm/modules/mamba_simple.py#L58
        dt_rank      = self.find_hparam(["mamba_dt_rank"], optional=True) or -(d_model // -16)
        rms_norm_eps = self.find_hparam(["layer_norm_epsilon", "rms_norm_eps"], optional=True) or 1e-6
        n_kv_head = self.hparams["num_key_value_heads"]
        attn_offset = self.hparams["attn_layer_offset"]
        attn_period = self.hparams["attn_layer_period"]
        n_kv_vec = [0 for _ in range(attn_offset)] + [
            n_kv_head if (i - attn_offset) % attn_period == 0 else 0 for i in range(attn_offset, self.block_count)
        ]

        self.gguf_writer.add_block_count(self.block_count)
        self.gguf_writer.add_context_length(self.find_hparam(["max_position_embeddings", "n_ctx"]))
        self.gguf_writer.add_embedding_length(d_model)
        self.gguf_writer.add_feed_forward_length(self.hparams["intermediate_size"])
        self.gguf_writer.add_head_count(self.hparams["num_attention_heads"])
        self.gguf_writer.add_head_count_kv(n_kv_vec)
        self.gguf_writer.add_ssm_conv_kernel(d_conv)
        self.gguf_writer.add_ssm_inner_size(d_inner)
        self.gguf_writer.add_ssm_state_size(d_state)
        self.gguf_writer.add_ssm_time_step_rank(dt_rank)
        self.gguf_writer.add_layer_norm_rms_eps(rms_norm_eps)
        self.gguf_writer.add_expert_count(self.hparams["num_experts"])
        self.gguf_writer.add_expert_used_count(self.hparams["num_experts_per_tok"])
        self.gguf_writer.add_file_type(self.ftype)

    _experts: list[dict[str, Tensor]] | None = None

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:

        # Mini-Jamba
        name = name.replace(".moe.", ".feed_forward.")
        if bid is not None:
            moe_offset = self.hparams["expert_layer_offset"]
            moe_period = self.hparams["expert_layer_period"]

            if not (bid >= moe_offset and (bid - moe_offset) % moe_period == 0):
                name = name.replace(".experts.0.", ".")

        # process the experts separately
        if ".feed_forward.experts." in name:
            n_experts = self.hparams["num_experts"]

            assert bid is not None

            if self._experts is None:
                self._experts = [{} for _ in range(self.block_count)]

            self._experts[bid][name] = data_torch

            if len(self._experts[bid]) >= n_experts * 3:

                # merge the experts into a single 3d tensor
                for wid in ["down_proj", "gate_proj", "up_proj"]:
                    datas: list[Tensor] = []

                    for xid in range(n_experts):
                        ename = f"model.layers.{bid}.feed_forward.experts.{xid}.{wid}.weight"
                        datas.append(self._experts[bid][ename])
                        del self._experts[bid][ename]

                    data_torch = torch.stack(datas, dim=0)

                    # using the same merged name as qwen2moe
                    merged_name = f"model.layers.{bid}.mlp.experts.{wid}.weight"

                    new_name = self.map_tensor_name(merged_name)

                    yield new_name, data_torch
            return

        new_name = self.map_tensor_name(name)

        if self.match_model_tensor_name(new_name, gguf.MODEL_TENSOR.SSM_CONV1D, bid):
            data_torch = data_torch.squeeze()

        if name.endswith(".A_log"):
            logger.debug("A_log --> A ==> " + new_name)
            data_torch = -torch.exp(data_torch)

        yield (new_name, data_torch)

    def prepare_tensors(self):
        super().prepare_tensors()

        if self._experts is not None:
            # flatten `list[dict[str, Tensor]]` into `list[str]`
            experts = [k for d in self._experts for k in d.keys()]
            if len(experts) > 0:
                raise ValueError(f"Unprocessed experts: {experts}")


@ModelBase.register("CohereForCausalLM")
class CommandR2Model(TextModel):
    model_arch = gguf.MODEL_ARCH.COMMAND_R

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # max_position_embeddings = 8192 in config.json but model was actually
        # trained on 128k context length
        # aya-23 models don't have model_max_length specified
        self.hparams["max_position_embeddings"] = self.find_hparam(["model_max_length", "max_position_embeddings"])

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_logit_scale(self.hparams["logit_scale"])
        self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.NONE)


@ModelBase.register("Cohere2ForCausalLM")
class Cohere2Model(TextModel):
    model_arch = gguf.MODEL_ARCH.COHERE2

    def set_gguf_parameters(self):
        super().set_gguf_parameters()

        self.gguf_writer.add_logit_scale(self.hparams["logit_scale"])
        self.gguf_writer.add_sliding_window(self.hparams["sliding_window"])
        self.gguf_writer.add_vocab_size(self.hparams["vocab_size"])

        rotary_pct = self.hparams["rotary_pct"]
        hidden_size = self.hparams["hidden_size"]
        num_attention_heads = self.hparams["num_attention_heads"]
        self.gguf_writer.add_rope_dimension_count(int(rotary_pct * (hidden_size // num_attention_heads)))
        self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.NONE)


@ModelBase.register("OlmoForCausalLM")
@ModelBase.register("OLMoForCausalLM")
class OlmoModel(TextModel):
    model_arch = gguf.MODEL_ARCH.OLMO

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_layer_norm_eps(1e-5)
        clip_qkv = self.hparams.get("clip_qkv")
        if clip_qkv is not None:
            self.gguf_writer.add_clamp_kqv(clip_qkv)

    # Same as super class, but permuting q_proj, k_proj
    # Copied from: LlamaModel
    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        n_head = self.hparams["num_attention_heads"]
        n_kv_head = self.hparams.get("num_key_value_heads")

        if name.endswith("q_proj.weight"):
            data_torch = LlamaModel.permute(data_torch, n_head, n_head)
        if name.endswith("k_proj.weight"):
            data_torch = LlamaModel.permute(data_torch, n_head, n_kv_head)

        return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("Olmo2ForCausalLM")
class Olmo2Model(TextModel):
    model_arch = gguf.MODEL_ARCH.OLMO2


@ModelBase.register("OlmoeForCausalLM")
class OlmoeModel(TextModel):
    model_arch = gguf.MODEL_ARCH.OLMOE

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_layer_norm_rms_eps(1e-5)
        if (n_experts := self.hparams.get("num_experts")) is not None:
            self.gguf_writer.add_expert_count(n_experts)

    _experts: list[dict[str, Tensor]] | None = None

    # Copied from: Qwen2MoeModel
    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        # process the experts separately
        if name.find("experts") != -1:
            n_experts = self.hparams["num_experts"]
            assert bid is not None

            if self._experts is None:
                self._experts = [{} for _ in range(self.block_count)]

            self._experts[bid][name] = data_torch

            if len(self._experts[bid]) >= n_experts * 3:
                tensors: list[tuple[str, Tensor]] = []

                # merge the experts into a single 3d tensor
                for w_name in ["down_proj", "gate_proj", "up_proj"]:
                    datas: list[Tensor] = []

                    for xid in range(n_experts):
                        ename = f"model.layers.{bid}.mlp.experts.{xid}.{w_name}.weight"
                        datas.append(self._experts[bid][ename])
                        del self._experts[bid][ename]

                    data_torch = torch.stack(datas, dim=0)

                    merged_name = f"model.layers.{bid}.mlp.experts.{w_name}.weight"

                    new_name = self.map_tensor_name(merged_name)

                    tensors.append((new_name, data_torch))
                return tensors
            else:
                return []

        return [(self.map_tensor_name(name), data_torch)]

    # Copied from: Qwen2MoeModel
    def prepare_tensors(self):
        super().prepare_tensors()

        if self._experts is not None:
            # flatten `list[dict[str, Tensor]]` into `list[str]`
            experts = [k for d in self._experts for k in d.keys()]
            if len(experts) > 0:
                raise ValueError(f"Unprocessed experts: {experts}")


@ModelBase.register("JinaBertModel", "JinaBertForMaskedLM")
class JinaBertV2Model(BertModel):
    model_arch = gguf.MODEL_ARCH.JINA_BERT_V2

    def set_vocab(self):
        tokenizer_class = 'BertTokenizer'
        with open(self.dir_model / "tokenizer_config.json", "r", encoding="utf-8") as f:
            tokenizer_class = json.load(f)['tokenizer_class']

        if tokenizer_class == 'BertTokenizer':
            super().set_vocab()
        elif tokenizer_class == 'RobertaTokenizer':
            self._set_vocab_gpt2()
            self.gguf_writer.add_token_type_count(2)
        else:
            raise NotImplementedError(f'Tokenizer {tokenizer_class} is not supported for JinaBertModel')


@ModelBase.register("OpenELMForCausalLM")
class OpenELMModel(TextModel):
    model_arch = gguf.MODEL_ARCH.OPENELM

    @staticmethod
    def _make_divisible(v: float | int, divisor: int) -> int:
        # ref: https://huggingface.co/apple/OpenELM-270M-Instruct/blob/eb111ff2e6724348e5b905984063d4064d4bc579/configuration_openelm.py#L34-L38
        new_v = max(divisor, int(v + divisor / 2) // divisor * divisor)
        # Make sure that round down does not go down by more than 10%.
        if new_v < 0.9 * v:
            new_v += divisor
        return new_v

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        ffn_multipliers: list[float] = self.hparams["ffn_multipliers"]
        ffn_dim_divisor: int = self.hparams["ffn_dim_divisor"]
        self._n_embd: int = self.hparams["model_dim"]
        self._num_kv_heads: list[int] = self.hparams["num_kv_heads"]
        self._num_query_heads: list[int] = self.hparams["num_query_heads"]
        self._ffn_dims: list[int] = [
            OpenELMModel._make_divisible(multiplier * self._n_embd, ffn_dim_divisor)
            for multiplier in ffn_multipliers
        ]
        assert isinstance(self._num_kv_heads, list) and isinstance(self._num_kv_heads[0], int)
        assert isinstance(self._num_query_heads, list) and isinstance(self._num_query_heads[0], int)

    # Uses the tokenizer from meta-llama/Llama-2-7b-hf
    def set_vocab(self):
        try:
            self._set_vocab_sentencepiece()
        except FileNotFoundError:
            self._set_vocab_builtin("llama-spm", self.hparams["vocab_size"])

    def set_gguf_parameters(self):
        n_embd = self._n_embd
        head_dim = self.hparams["head_dim"]
        rot_pct = 1.0
        assert self.block_count == len(self._num_kv_heads)
        assert self.block_count == len(self._num_query_heads)
        assert self.block_count == len(self._ffn_dims)

        self.gguf_writer.add_block_count(self.block_count)
        self.gguf_writer.add_context_length(self.hparams["max_context_length"])
        self.gguf_writer.add_embedding_length(n_embd)
        self.gguf_writer.add_feed_forward_length(self._ffn_dims)
        self.gguf_writer.add_head_count(self._num_query_heads)
        self.gguf_writer.add_head_count_kv(self._num_kv_heads)
        self.gguf_writer.add_rope_freq_base(self.hparams["rope_freq_constant"])
        # https://huggingface.co/apple/OpenELM-270M-Instruct/blob/c401df2/modeling_openelm.py#L30
        self.gguf_writer.add_layer_norm_rms_eps(1e-6)
        self.gguf_writer.add_rope_dimension_count(int(rot_pct * head_dim))
        self.gguf_writer.add_key_length(head_dim)
        self.gguf_writer.add_value_length(head_dim)
        self.gguf_writer.add_file_type(self.ftype)

    def find_hparam(self, keys: Iterable[str], optional: bool = False) -> Any:
        if "n_layers" in keys:
            return self.hparams["num_transformer_layers"]

        return super().find_hparam(keys, optional)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:

        # split ff
        if bid is not None and name == f"transformer.layers.{bid}.ffn.proj_1.weight":
            ff_dim = self._ffn_dims[bid]
            yield (self.format_tensor_name(gguf.MODEL_TENSOR.FFN_GATE, bid), data_torch[:ff_dim])
            yield (self.format_tensor_name(gguf.MODEL_TENSOR.FFN_UP, bid), data_torch[ff_dim:])
            return

        yield (self.map_tensor_name(name), data_torch)


@ModelBase.register("ArcticForCausalLM")
class ArcticModel(TextModel):
    model_arch = gguf.MODEL_ARCH.ARCTIC

    def set_vocab(self):
        # The reason for using a custom implementation here is that the
        # snowflake-arctic-instruct model redefined tokens 31998 and 31999 from
        # tokenizer.model and used them as BOS and EOS instead of adding new tokens.
        from sentencepiece import SentencePieceProcessor

        tokenizer_path = self.dir_model / 'tokenizer.model'

        if not tokenizer_path.is_file():
            logger.error(f'Error: Missing {tokenizer_path}')
            sys.exit(1)

        # Read the whole vocabulary from the tokenizer.model file
        tokenizer = SentencePieceProcessor()
        tokenizer.LoadFromFile(str(tokenizer_path))

        vocab_size = self.hparams.get('vocab_size', tokenizer.vocab_size())

        tokens: list[bytes] = [f"[PAD{i}]".encode("utf-8") for i in range(vocab_size)]
        scores: list[float] = [-10000.0] * vocab_size
        toktypes: list[int] = [SentencePieceTokenTypes.UNUSED] * vocab_size

        for token_id in range(tokenizer.vocab_size()):

            piece = tokenizer.IdToPiece(token_id)
            text = piece.encode("utf-8")
            score = tokenizer.GetScore(token_id)

            toktype = SentencePieceTokenTypes.NORMAL
            if tokenizer.IsUnknown(token_id):
                toktype = SentencePieceTokenTypes.UNKNOWN
            elif tokenizer.IsControl(token_id):
                toktype = SentencePieceTokenTypes.CONTROL
            elif tokenizer.IsUnused(token_id):
                toktype = SentencePieceTokenTypes.UNUSED
            elif tokenizer.IsByte(token_id):
                toktype = SentencePieceTokenTypes.BYTE

            tokens[token_id] = text
            scores[token_id] = score
            toktypes[token_id] = toktype

        # Use the added_tokens_decoder field from tokeniser_config.json as the source
        # of information about added/redefined tokens and modify them accordingly.
        tokenizer_config_file = self.dir_model / 'tokenizer_config.json'
        if tokenizer_config_file.is_file():
            with open(tokenizer_config_file, "r", encoding="utf-8") as f:
                tokenizer_config_json = json.load(f)

                if "added_tokens_decoder" in tokenizer_config_json:
                    added_tokens_decoder = tokenizer_config_json["added_tokens_decoder"]
                    for token_id, token_json in added_tokens_decoder.items():
                        token_id = int(token_id)
                        if token_id >= vocab_size:
                            logger.debug(f'ignore token {token_id}: id is out of range, max={vocab_size - 1}')
                            continue

                        token_content = token_json["content"]
                        token_type = SentencePieceTokenTypes.USER_DEFINED
                        token_score = -10000.0

                        # Map unk_token to UNKNOWN, other special tokens to CONTROL
                        # Set the score to 0.0 as in the original tokenizer.model
                        if ("special" in token_json) and token_json["special"]:
                            if token_content == tokenizer_config_json["unk_token"]:
                                token_type = SentencePieceTokenTypes.UNKNOWN
                            else:
                                token_type = SentencePieceTokenTypes.CONTROL
                            token_score = 0.0

                        logger.info(f"Setting added token {token_id} to '{token_content}' (type: {token_type}, score: {token_score:.2f})")
                        tokens[token_id] = token_content.encode("utf-8")
                        toktypes[token_id] = token_type
                        scores[token_id] = token_score

        self.gguf_writer.add_tokenizer_model("llama")
        self.gguf_writer.add_tokenizer_pre("default")
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_scores(scores)
        self.gguf_writer.add_token_types(toktypes)

        special_vocab = gguf.SpecialVocab(self.dir_model, n_vocab=len(tokens))
        special_vocab.add_to_gguf(self.gguf_writer)

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        hparams = self.hparams
        self.gguf_writer.add_vocab_size(hparams["vocab_size"])
        self.gguf_writer.add_rope_dimension_count(hparams["hidden_size"] // hparams["num_attention_heads"])

    _experts: list[dict[str, Tensor]] | None = None

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        n_head = self.hparams["num_attention_heads"]
        n_kv_head = self.hparams.get("num_key_value_heads")

        if name.endswith("q_proj.weight"):
            data_torch = LlamaModel.permute(data_torch, n_head, n_head)
        if name.endswith("k_proj.weight"):
            data_torch = LlamaModel.permute(data_torch, n_head, n_kv_head)

        # process the experts separately
        if name.find("block_sparse_moe.experts") != -1:
            n_experts = self.hparams["num_local_experts"]

            assert bid is not None

            if self._experts is None:
                self._experts = [{} for _ in range(self.block_count)]

            self._experts[bid][name] = data_torch

            if len(self._experts[bid]) >= n_experts * 3:
                tensors: list[tuple[str, Tensor]] = []

                # merge the experts into a single 3d tensor
                for wid in ["w1", "w2", "w3"]:
                    datas: list[Tensor] = []

                    for xid in range(n_experts):
                        ename = f"model.layers.{bid}.block_sparse_moe.experts.{xid}.{wid}.weight"
                        datas.append(self._experts[bid][ename])
                        del self._experts[bid][ename]

                    data_torch = torch.stack(datas, dim=0)

                    merged_name = f"layers.{bid}.feed_forward.experts.{wid}.weight"

                    new_name = self.map_tensor_name(merged_name)

                    tensors.append((new_name, data_torch))
                return tensors
            else:
                return []

        return [(self.map_tensor_name(name), data_torch)]

    def prepare_tensors(self):
        super().prepare_tensors()

        if self._experts is not None:
            # flatten `list[dict[str, Tensor]]` into `list[str]`
            experts = [k for d in self._experts for k in d.keys()]
            if len(experts) > 0:
                raise ValueError(f"Unprocessed experts: {experts}")


@ModelBase.register("DeepseekForCausalLM")
class DeepseekModel(TextModel):
    model_arch = gguf.MODEL_ARCH.DEEPSEEK

    def set_vocab(self):
        try:
            self._set_vocab_sentencepiece()
        except FileNotFoundError:
            self._set_vocab_gpt2()

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        hparams = self.hparams
        if (rope_dim := hparams.get("head_dim")) is None:
            rope_dim = hparams["hidden_size"] // hparams["num_attention_heads"]

        self.gguf_writer.add_rope_dimension_count(rope_dim)
        self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.NONE)
        self.gguf_writer.add_leading_dense_block_count(hparams["first_k_dense_replace"])
        self.gguf_writer.add_vocab_size(hparams["vocab_size"])
        self.gguf_writer.add_expert_feed_forward_length(hparams["moe_intermediate_size"])
        self.gguf_writer.add_expert_weights_scale(1.0)
        self.gguf_writer.add_expert_count(hparams["n_routed_experts"])
        self.gguf_writer.add_expert_shared_count(hparams["n_shared_experts"])

    _experts: list[dict[str, Tensor]] | None = None

    @staticmethod
    def permute(weights: Tensor, n_head: int, n_head_kv: int | None):
        if n_head_kv is not None and n_head != n_head_kv:
            n_head = n_head_kv
        return (weights.reshape(n_head, 2, weights.shape[0] // n_head // 2, *weights.shape[1:])
                .swapaxes(1, 2)
                .reshape(weights.shape))

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        n_head = self.hparams["num_attention_heads"]
        n_kv_head = self.hparams.get("num_key_value_heads")

        if name.endswith(("q_proj.weight", "q_proj.bias")):
            data_torch = DeepseekModel.permute(data_torch, n_head, n_head)
        if name.endswith(("k_proj.weight", "k_proj.bias")):
            data_torch = DeepseekModel.permute(data_torch, n_head, n_kv_head)

        # process the experts separately
        if name.find("mlp.experts") != -1:
            n_experts = self.hparams["n_routed_experts"]
            assert bid is not None

            if self._experts is None:
                self._experts = [{} for _ in range(self.block_count)]

            self._experts[bid][name] = data_torch

            if len(self._experts[bid]) >= n_experts * 3:
                tensors: list[tuple[str, Tensor]] = []

                # merge the experts into a single 3d tensor
                for w_name in ["down_proj", "gate_proj", "up_proj"]:
                    datas: list[Tensor] = []

                    for xid in range(n_experts):
                        ename = f"model.layers.{bid}.mlp.experts.{xid}.{w_name}.weight"
                        datas.append(self._experts[bid][ename])
                        del self._experts[bid][ename]

                    data_torch = torch.stack(datas, dim=0)

                    merged_name = f"model.layers.{bid}.mlp.experts.{w_name}.weight"

                    new_name = self.map_tensor_name(merged_name)

                    tensors.append((new_name, data_torch))
                return tensors
            else:
                return []

        return [(self.map_tensor_name(name), data_torch)]

    def prepare_tensors(self):
        super().prepare_tensors()

        if self._experts is not None:
            # flatten `list[dict[str, Tensor]]` into `list[str]`
            experts = [k for d in self._experts for k in d.keys()]
            if len(experts) > 0:
                raise ValueError(f"Unprocessed experts: {experts}")


@ModelBase.register("DeepseekV2ForCausalLM")
@ModelBase.register("DeepseekV3ForCausalLM")
class DeepseekV2Model(TextModel):
    model_arch = gguf.MODEL_ARCH.DEEPSEEK2

    def set_vocab(self):
        try:
            self._set_vocab_gpt2()
            return
        except Exception:
            pass

        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(self.dir_model, trust_remote_code=True)
        tokpre = self.get_vocab_base_pre(tokenizer)

        if tokpre == "kimi-k2":
            # Build merges list using the approach similar to HunYuanMoE
            merges = []
            vocab = {}
            mergeable_ranks = tokenizer.model._mergeable_ranks
            for token, rank in mergeable_ranks.items():
                vocab[QwenModel.token_bytes_to_string(token)] = rank
                if len(token) == 1:
                    continue
                merged = QwenModel.bpe(mergeable_ranks, token, max_rank=rank)
                if len(merged) == 2:
                    merges.append(' '.join(map(QwenModel.token_bytes_to_string, merged)))

            # Build token list
            vocab_size = self.hparams["vocab_size"]
            special_tokens = tokenizer.special_tokens
            reverse_vocab = {id_ : encoded_tok for encoded_tok, id_ in {**vocab, **special_tokens}.items()}
            tokens: list[str] = []
            toktypes: list[int] = []

            for i in range(vocab_size):
                if i not in reverse_vocab:
                    tokens.append(f"[PAD{i}]")
                    toktypes.append(gguf.TokenType.UNUSED)
                else:
                    token = reverse_vocab[i]
                    tokens.append(token)
                    if i in special_tokens.values():
                        toktypes.append(gguf.TokenType.CONTROL)
                    else:
                        toktypes.append(gguf.TokenType.NORMAL)

            self.gguf_writer.add_tokenizer_model("gpt2")
            self.gguf_writer.add_tokenizer_pre(tokpre)
            self.gguf_writer.add_token_list(tokens)
            self.gguf_writer.add_token_types(toktypes)
            self.gguf_writer.add_token_merges(merges)

            special_vocab = gguf.SpecialVocab(self.dir_model, load_merges=False)
            special_vocab.add_to_gguf(self.gguf_writer)
        else:
            raise NotImplementedError(f"Deepseek pre-tokenizer {tokpre!r} is not supported yet!")

    def set_gguf_parameters(self):

        # note: deepseek2 using MLA converts into MQA (ie: GQA with 1 group)
        self.hparams["num_key_value_heads"] = 1

        super().set_gguf_parameters()
        hparams = self.hparams

        self.gguf_writer.add_leading_dense_block_count(hparams["first_k_dense_replace"])
        self.gguf_writer.add_vocab_size(hparams["vocab_size"])
        if "q_lora_rank" in hparams and hparams["q_lora_rank"] is not None:
            self.gguf_writer.add_q_lora_rank(hparams["q_lora_rank"])
        self.gguf_writer.add_kv_lora_rank(hparams["kv_lora_rank"])

        # note: deepseek2 using MLA converts into MQA with larger heads, then decompresses to MHA
        self.gguf_writer.add_key_length(hparams["kv_lora_rank"] + hparams["qk_rope_head_dim"])
        self.gguf_writer.add_value_length(hparams["kv_lora_rank"])
        self.gguf_writer.add_key_length_mla(hparams["qk_nope_head_dim"] + hparams["qk_rope_head_dim"])
        self.gguf_writer.add_value_length_mla(hparams["v_head_dim"])

        self.gguf_writer.add_expert_feed_forward_length(hparams["moe_intermediate_size"])
        self.gguf_writer.add_expert_count(hparams["n_routed_experts"])
        self.gguf_writer.add_expert_shared_count(hparams["n_shared_experts"])
        self.gguf_writer.add_expert_weights_scale(hparams["routed_scaling_factor"])
        self.gguf_writer.add_expert_weights_norm(hparams["norm_topk_prob"])

        if hparams["scoring_func"] == "sigmoid":
            self.gguf_writer.add_expert_gating_func(gguf.ExpertGatingFuncType.SIGMOID)
        elif hparams["scoring_func"] == "softmax":
            self.gguf_writer.add_expert_gating_func(gguf.ExpertGatingFuncType.SOFTMAX)
        else:
            raise ValueError(f"Unsupported scoring_func value: {hparams['scoring_func']}")

        self.gguf_writer.add_rope_dimension_count(hparams["qk_rope_head_dim"])

        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "yarn" and "factor" in rope_scaling:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.YARN)
            self.gguf_writer.add_rope_scaling_factor(rope_scaling["factor"])
            self.gguf_writer.add_rope_scaling_orig_ctx_len(rope_scaling["original_max_position_embeddings"])
            self.gguf_writer.add_rope_scaling_yarn_log_mul(0.1 * rope_scaling["mscale_all_dim"])

    _experts: list[dict[str, Tensor]] | None = None

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        # rename e_score_correction_bias tensors
        if name.endswith("e_score_correction_bias"):
            name = name.replace("e_score_correction_bias", "e_score_correction.bias")

        # skip Multi-Token Prediction (MTP) layers
        block_count = self.hparams["num_hidden_layers"]
        match = re.match(r"model.layers.(\d+)", name)
        if match and int(match.group(1)) >= block_count:
            return []

        # process the experts separately
        if name.find("mlp.experts") != -1:
            n_experts = self.hparams["n_routed_experts"]
            assert bid is not None

            if self._experts is None:
                self._experts = [{} for _ in range(self.block_count)]

            self._experts[bid][name] = data_torch

            if len(self._experts[bid]) >= n_experts * 3:
                tensors: list[tuple[str, Tensor]] = []

                # merge the experts into a single 3d tensor
                for w_name in ["down_proj", "gate_proj", "up_proj"]:
                    datas: list[Tensor] = []

                    for xid in range(n_experts):
                        ename = f"model.layers.{bid}.mlp.experts.{xid}.{w_name}.weight"
                        datas.append(self._experts[bid][ename])
                        del self._experts[bid][ename]

                    data_torch = torch.stack(datas, dim=0)

                    merged_name = f"model.layers.{bid}.mlp.experts.{w_name}.weight"

                    new_name = self.map_tensor_name(merged_name)

                    tensors.append((new_name, data_torch))
                return tensors
            else:
                return []

        # note: MLA with the absorption optimization, needs these two split and k_b_proj transposed
        if name.endswith("kv_b_proj.weight"):
            name_kb = name.replace("kv_b_proj", "k_b_proj")
            name_vb = name.replace("kv_b_proj", "v_b_proj")

            n_head_kv = self.hparams["num_key_value_heads"]
            v_head_dim = self.hparams["v_head_dim"]
            qk_nope_head_dim = self.hparams["qk_nope_head_dim"]

            assert data_torch.shape[0] == n_head_kv * (v_head_dim + qk_nope_head_dim)

            kv_b = data_torch.view(n_head_kv, v_head_dim + qk_nope_head_dim, data_torch.shape[-1])
            k_b, v_b = torch.split(kv_b, [qk_nope_head_dim, v_head_dim], dim=1)
            k_b = k_b.transpose(1, 2)

            return [
                (self.map_tensor_name(name_kb), k_b),
                (self.map_tensor_name(name_vb), v_b)
            ]

        return [(self.map_tensor_name(name), data_torch)]

    def prepare_tensors(self):
        super().prepare_tensors()

        if self._experts is not None:
            # flatten `list[dict[str, Tensor]]` into `list[str]`
            experts = [k for d in self._experts for k in d.keys()]
            if len(experts) > 0:
                raise ValueError(f"Unprocessed experts: {experts}")


@ModelBase.register("Dots1ForCausalLM")
class Dots1Model(Qwen2MoeModel):
    model_arch = gguf.MODEL_ARCH.DOTS1

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hparams["num_experts"] = self.hparams["n_routed_experts"]

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_leading_dense_block_count(self.hparams["first_k_dense_replace"])
        self.gguf_writer.add_expert_shared_count(self.hparams["n_shared_experts"])
        self.gguf_writer.add_expert_weights_scale(self.hparams["routed_scaling_factor"])
        self.gguf_writer.add_expert_weights_norm(self.hparams["norm_topk_prob"])

        if self.hparams["scoring_func"] == "noaux_tc":
            self.gguf_writer.add_expert_gating_func(gguf.ExpertGatingFuncType.SIGMOID)
        else:
            raise ValueError(f"Unsupported scoring_func value: {self.hparams['scoring_func']}")

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None):
        if name.endswith("e_score_correction_bias"):
            name = name.replace("e_score_correction_bias", "e_score_correction.bias")
        if "shared_experts" in name:
            return [(self.map_tensor_name(name), data_torch)]
        return super().modify_tensors(data_torch, name, bid)


@ModelBase.register("PLMForCausalLM")
class PLMModel(TextModel):
    model_arch = gguf.MODEL_ARCH.PLM

    def set_vocab(self):
        self._set_vocab_gpt2()

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        hparams = self.hparams
        self.gguf_writer.add_vocab_size(hparams["vocab_size"])
        self.gguf_writer.add_kv_lora_rank(hparams["kv_lora_rank"])
        self.gguf_writer.add_key_length(hparams["qk_nope_head_dim"] + hparams["qk_rope_head_dim"])
        self.gguf_writer.add_value_length(hparams["v_head_dim"])
        self.gguf_writer.add_rope_dimension_count(hparams["qk_rope_head_dim"])

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        return [(self.map_tensor_name(name), data_torch)]

    def prepare_tensors(self):
        super().prepare_tensors()


@ModelBase.register("T5WithLMHeadModel")
@ModelBase.register("T5ForConditionalGeneration")
@ModelBase.register("MT5ForConditionalGeneration")
@ModelBase.register("UMT5ForConditionalGeneration")
class T5Model(TextModel):
    model_arch = gguf.MODEL_ARCH.T5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.shared_token_embeddings_found = False

    def set_vocab(self):
        # to avoid TypeError: Descriptors cannot be created directly
        # exception when importing sentencepiece_model_pb2
        os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
        from sentencepiece import SentencePieceProcessor
        from sentencepiece import sentencepiece_model_pb2 as model

        tokenizer_path = self.dir_model / 'tokenizer.model'

        # many older models use spiece.model tokenizer model filename
        if not tokenizer_path.is_file():
            tokenizer_path = self.dir_model / 'spiece.model'

        if not tokenizer_path.is_file():
            raise FileNotFoundError(f"File not found: {tokenizer_path}")

        sentencepiece_model = model.ModelProto()  # pyright: ignore[reportAttributeAccessIssue]
        sentencepiece_model.ParseFromString(open(tokenizer_path, "rb").read())

        # some models like Pile-T5 family use BPE tokenizer instead of Unigram
        if sentencepiece_model.trainer_spec.model_type == 2:  # BPE
            # assure the tokenizer model file name is correct
            assert tokenizer_path.name == 'tokenizer.model'
            return self._set_vocab_sentencepiece()
        else:
            assert sentencepiece_model.trainer_spec.model_type == 1  # UNIGRAM

        add_prefix = sentencepiece_model.normalizer_spec.add_dummy_prefix
        remove_whitespaces = sentencepiece_model.normalizer_spec.remove_extra_whitespaces
        precompiled_charsmap = sentencepiece_model.normalizer_spec.precompiled_charsmap

        tokenizer = SentencePieceProcessor()
        tokenizer.LoadFromFile(str(tokenizer_path))

        vocab_size = self.hparams.get('vocab_size', tokenizer.vocab_size())

        tokens: list[bytes] = [f"[PAD{i}]".encode("utf-8") for i in range(vocab_size)]
        scores: list[float] = [-10000.0] * vocab_size
        toktypes: list[int] = [SentencePieceTokenTypes.UNUSED] * vocab_size

        for token_id in range(tokenizer.vocab_size()):
            piece = tokenizer.IdToPiece(token_id)
            text = piece.encode("utf-8")
            score = tokenizer.GetScore(token_id)

            toktype = SentencePieceTokenTypes.NORMAL
            if tokenizer.IsUnknown(token_id):
                toktype = SentencePieceTokenTypes.UNKNOWN
            elif tokenizer.IsControl(token_id):
                toktype = SentencePieceTokenTypes.CONTROL
            elif tokenizer.IsUnused(token_id):
                toktype = SentencePieceTokenTypes.UNUSED
            elif tokenizer.IsByte(token_id):
                toktype = SentencePieceTokenTypes.BYTE

            tokens[token_id] = text
            scores[token_id] = score
            toktypes[token_id] = toktype

        added_tokens_file = self.dir_model / 'added_tokens.json'
        if added_tokens_file.is_file():
            with open(added_tokens_file, "r", encoding="utf-8") as f:
                added_tokens_json = json.load(f)
                for key in added_tokens_json:
                    token_id = added_tokens_json[key]
                    if token_id >= vocab_size:
                        logger.warning(f'ignore token {token_id}: id is out of range, max={vocab_size - 1}')
                        continue

                    tokens[token_id] = key.encode("utf-8")
                    scores[token_id] = -1000.0
                    toktypes[token_id] = SentencePieceTokenTypes.USER_DEFINED

        if vocab_size > len(tokens):
            pad_count = vocab_size - len(tokens)
            logger.debug(f"Padding vocab with {pad_count} token(s) - [PAD1] through [PAD{pad_count}]")
            for i in range(1, pad_count + 1):
                tokens.append(bytes(f"[PAD{i}]", encoding="utf-8"))
                scores.append(-1000.0)
                toktypes.append(SentencePieceTokenTypes.UNUSED)

        self.gguf_writer.add_tokenizer_model("t5")
        self.gguf_writer.add_tokenizer_pre("default")
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_scores(scores)
        self.gguf_writer.add_token_types(toktypes)
        self.gguf_writer.add_add_space_prefix(add_prefix)
        self.gguf_writer.add_remove_extra_whitespaces(remove_whitespaces)
        if precompiled_charsmap:
            self.gguf_writer.add_precompiled_charsmap(precompiled_charsmap)

        special_vocab = gguf.SpecialVocab(self.dir_model, n_vocab=len(tokens))
        special_vocab.add_to_gguf(self.gguf_writer)

    def set_gguf_parameters(self):
        if (n_ctx := self.find_hparam(["n_positions"], optional=True)) is None:
            logger.warning("Couldn't find context length in config.json, assuming default value of 512")
            n_ctx = 512
        self.gguf_writer.add_context_length(n_ctx)
        self.gguf_writer.add_embedding_length(self.hparams["d_model"])
        self.gguf_writer.add_feed_forward_length(self.hparams["d_ff"])
        self.gguf_writer.add_block_count(self.hparams["num_layers"])
        self.gguf_writer.add_head_count(self.hparams["num_heads"])
        self.gguf_writer.add_key_length(self.hparams["d_kv"])
        self.gguf_writer.add_value_length(self.hparams["d_kv"])
        self.gguf_writer.add_layer_norm_eps(self.hparams["layer_norm_epsilon"])
        self.gguf_writer.add_relative_attn_buckets_count(self.hparams["relative_attention_num_buckets"])
        self.gguf_writer.add_layer_norm_rms_eps(self.hparams["layer_norm_epsilon"])
        self.gguf_writer.add_decoder_start_token_id(self.hparams["decoder_start_token_id"])
        self.gguf_writer.add_file_type(self.ftype)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        # T5 based models contain shared token embeddings tensors saved randomly as either "encoder.embed_tokens.weight",
        # "decoder.embed_tokens.weight" or "shared.weight" tensor. In some models there are even multiple of them stored
        # in the safetensors files. We use the first tensor from these three as the token embeddings for both encoder
        # and decoder and ignore the remaining ones.
        if name in ["decoder.embed_tokens.weight", "encoder.embed_tokens.weight", "shared.weight"]:
            if not self.shared_token_embeddings_found:
                name = "shared.weight"
                self.shared_token_embeddings_found = True
            else:
                logger.debug(f"Skipping shared tensor {name!r} in safetensors so that convert can end normally.")
                return []

        return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("T5EncoderModel")
class T5EncoderModel(TextModel):
    model_arch = gguf.MODEL_ARCH.T5ENCODER

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.shared_token_embeddings_found = False

    def set_vocab(self):
        # to avoid TypeError: Descriptors cannot be created directly
        # exception when importing sentencepiece_model_pb2
        os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
        from sentencepiece import SentencePieceProcessor
        from sentencepiece import sentencepiece_model_pb2 as model

        tokenizer_path = self.dir_model / 'tokenizer.model'

        # many older models use spiece.model tokenizer model filename
        if not tokenizer_path.is_file():
            tokenizer_path = self.dir_model / 'spiece.model'

        if not tokenizer_path.is_file():
            raise FileNotFoundError(f"File not found: {tokenizer_path}")

        sentencepiece_model = model.ModelProto()  # pyright: ignore[reportAttributeAccessIssue]
        sentencepiece_model.ParseFromString(open(tokenizer_path, "rb").read())

        # some models like Pile-T5 family use BPE tokenizer instead of Unigram
        if sentencepiece_model.trainer_spec.model_type == 2:  # BPE
            # assure the tokenizer model file name is correct
            assert tokenizer_path.name == 'tokenizer.model'
            return self._set_vocab_sentencepiece()
        else:
            assert sentencepiece_model.trainer_spec.model_type == 1  # UNIGRAM

        add_prefix = sentencepiece_model.normalizer_spec.add_dummy_prefix
        remove_whitespaces = sentencepiece_model.normalizer_spec.remove_extra_whitespaces
        precompiled_charsmap = sentencepiece_model.normalizer_spec.precompiled_charsmap

        tokenizer = SentencePieceProcessor()
        tokenizer.LoadFromFile(str(tokenizer_path))

        vocab_size = self.hparams.get('vocab_size', tokenizer.vocab_size())

        tokens: list[bytes] = [f"[PAD{i}]".encode("utf-8") for i in range(vocab_size)]
        scores: list[float] = [-10000.0] * vocab_size
        toktypes: list[int] = [SentencePieceTokenTypes.UNUSED] * vocab_size

        for token_id in range(tokenizer.vocab_size()):
            piece = tokenizer.IdToPiece(token_id)
            text = piece.encode("utf-8")
            score = tokenizer.GetScore(token_id)

            toktype = SentencePieceTokenTypes.NORMAL
            if tokenizer.IsUnknown(token_id):
                toktype = SentencePieceTokenTypes.UNKNOWN
            elif tokenizer.IsControl(token_id):
                toktype = SentencePieceTokenTypes.CONTROL
            elif tokenizer.IsUnused(token_id):
                toktype = SentencePieceTokenTypes.UNUSED
            elif tokenizer.IsByte(token_id):
                toktype = SentencePieceTokenTypes.BYTE

            tokens[token_id] = text
            scores[token_id] = score
            toktypes[token_id] = toktype

        added_tokens_file = self.dir_model / 'added_tokens.json'
        if added_tokens_file.is_file():
            with open(added_tokens_file, "r", encoding="utf-8") as f:
                added_tokens_json = json.load(f)
                for key in added_tokens_json:
                    token_id = added_tokens_json[key]
                    if token_id >= vocab_size:
                        logger.warning(f'ignore token {token_id}: id is out of range, max={vocab_size - 1}')
                        continue

                    tokens[token_id] = key.encode("utf-8")
                    scores[token_id] = -1000.0
                    toktypes[token_id] = SentencePieceTokenTypes.USER_DEFINED

        if vocab_size > len(tokens):
            pad_count = vocab_size - len(tokens)
            logger.debug(f"Padding vocab with {pad_count} token(s) - [PAD1] through [PAD{pad_count}]")
            for i in range(1, pad_count + 1):
                tokens.append(bytes(f"[PAD{i}]", encoding="utf-8"))
                scores.append(-1000.0)
                toktypes.append(SentencePieceTokenTypes.UNUSED)

        self.gguf_writer.add_tokenizer_model("t5")
        self.gguf_writer.add_tokenizer_pre("default")
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_scores(scores)
        self.gguf_writer.add_token_types(toktypes)
        self.gguf_writer.add_add_space_prefix(add_prefix)
        self.gguf_writer.add_remove_extra_whitespaces(remove_whitespaces)
        if precompiled_charsmap:
            self.gguf_writer.add_precompiled_charsmap(precompiled_charsmap)

        special_vocab = gguf.SpecialVocab(self.dir_model, n_vocab=len(tokens))
        special_vocab.add_to_gguf(self.gguf_writer)

    def set_gguf_parameters(self):
        if (n_ctx := self.find_hparam(["n_positions"], optional=True)) is None:
            logger.warning("Couldn't find context length in config.json, assuming default value of 512")
            n_ctx = 512
        self.gguf_writer.add_context_length(n_ctx)
        self.gguf_writer.add_embedding_length(self.hparams["d_model"])
        self.gguf_writer.add_feed_forward_length(self.hparams["d_ff"])
        self.gguf_writer.add_block_count(self.hparams["num_layers"])
        self.gguf_writer.add_head_count(self.hparams["num_heads"])
        self.gguf_writer.add_key_length(self.hparams["d_kv"])
        self.gguf_writer.add_value_length(self.hparams["d_kv"])
        self.gguf_writer.add_layer_norm_eps(self.hparams["layer_norm_epsilon"])
        self.gguf_writer.add_relative_attn_buckets_count(self.hparams["relative_attention_num_buckets"])
        self.gguf_writer.add_layer_norm_rms_eps(self.hparams["layer_norm_epsilon"])
        self.gguf_writer.add_file_type(self.ftype)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        # T5 based models contain shared token embeddings tensors saved randomly as either "encoder.embed_tokens.weight",
        # "decoder.embed_tokens.weight" or "shared.weight" tensor. In some models there are even multiple of them stored
        # in the safetensors files. We use the first tensor from these three as the token embeddings for both encoder
        # and decoder and ignore the remaining ones.
        if name in ["decoder.embed_tokens.weight", "encoder.embed_tokens.weight", "shared.weight"]:
            if not self.shared_token_embeddings_found:
                name = "shared.weight"
                self.shared_token_embeddings_found = True
            else:
                logger.debug(f"Skipping shared tensor {name!r} in safetensors so that convert can end normally.")
                return []

        return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("JAISLMHeadModel")
class JaisModel(TextModel):
    model_arch = gguf.MODEL_ARCH.JAIS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # SwigLU activation
        assert self.hparams["activation_function"] == "swiglu"
        # ALiBi position embedding
        assert self.hparams["position_embedding_type"] == "alibi"

        # Embeddings scale
        self.embeddings_scale = 1.0
        if 'mup_embeddings_scale' in self.hparams:
            self.embeddings_scale = self.hparams['mup_embeddings_scale']
        elif 'embeddings_scale' in self.hparams:
            self.embeddings_scale = self.hparams['embeddings_scale']
        else:
            assert False

        self.width_scale = 1.0
        if 'mup_output_alpha' in self.hparams:
            assert 'mup_width_scale' in self.hparams
            self.width_scale = self.hparams['mup_output_alpha'] * self.hparams['mup_width_scale']
        elif 'width_scale' in self.hparams:
            self.width_scale = self.hparams['width_scale']
        else:
            assert False

        self.max_alibi_bias = 8.0

    def set_vocab(self):
        self._set_vocab_gpt2()

    def set_gguf_parameters(self):
        self.gguf_writer.add_block_count(self.hparams["n_layer"])
        self.gguf_writer.add_context_length(self.hparams["n_positions"])
        self.gguf_writer.add_embedding_length(self.hparams["n_embd"])
        self.gguf_writer.add_feed_forward_length(self.hparams["n_inner"])
        self.gguf_writer.add_head_count(self.hparams["n_head"])
        self.gguf_writer.add_layer_norm_eps(self.hparams["layer_norm_epsilon"])
        self.gguf_writer.add_file_type(self.ftype)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        tensors: list[tuple[str, Tensor]] = []

        # we don't need these
        if name.endswith((".attn.bias")):
            return tensors

        if name.endswith(("relative_pe.slopes")):
            # Calculate max ALiBi bias (this is the inverse of the ALiBi calculation)
            # Some other models has max_alibi_bias spelled out explicitly in the hyperparams,
            # but Jais's PyTorch model simply precalculates the slope values and places them
            # in relative_pes.slopes
            n_head_closest_log2 = 2 ** math.floor(math.log2(self.hparams["n_head"]))
            first_val = float(data_torch[0].item())
            self.max_alibi_bias = -round(math.log2(first_val) * n_head_closest_log2)

            return tensors

        if name.endswith((".c_attn.weight", ".c_proj.weight", ".c_fc.weight", ".c_fc2.weight")):
            data_torch = data_torch.transpose(1, 0)

        new_name = self.map_tensor_name(name)

        if new_name == self.format_tensor_name(gguf.MODEL_TENSOR.TOKEN_EMBD):
            tensors.append((new_name, data_torch * self.embeddings_scale))
        elif new_name == self.format_tensor_name(gguf.MODEL_TENSOR.OUTPUT):
            tensors.append((new_name, data_torch * self.width_scale))
        else:
            tensors.append((new_name, data_torch))

        return tensors

    def prepare_tensors(self):
        super().prepare_tensors()
        self.gguf_writer.add_max_alibi_bias(self.max_alibi_bias)


@ModelBase.register("Glm4ForCausalLM", "Glm4vForConditionalGeneration")
class Glm4Model(TextModel):
    model_arch = gguf.MODEL_ARCH.GLM4

    def set_vocab(self):
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(self.dir_model, trust_remote_code=True)
        special_vocab = gguf.SpecialVocab(self.dir_model, load_merges=True)
        tokens, toktypes, tokpre = self.get_vocab_base()
        self.gguf_writer.add_tokenizer_model("gpt2")
        self.gguf_writer.add_tokenizer_pre(tokpre)
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_types(toktypes)
        special_vocab = gguf.SpecialVocab(self.dir_model, load_merges=True)
        special_vocab._set_special_token("eos", tokenizer.get_added_vocab()["<|endoftext|>"])
        special_vocab._set_special_token("eot", tokenizer.get_added_vocab()["<|user|>"])
        special_vocab._set_special_token("unk", tokenizer.get_added_vocab()["<|endoftext|>"])
        special_vocab._set_special_token("bos", tokenizer.get_added_vocab()["<|endoftext|>"])
        special_vocab.add_to_gguf(self.gguf_writer)

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        if (rope_dim := self.hparams.get("head_dim")) is None:
            rope_dim = self.hparams["hidden_size"] // self.hparams["num_attention_heads"]
        self.gguf_writer.add_rope_dimension_count(int(rope_dim * self.hparams.get("partial_rotary_factor", 0.5)))
        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "yarn" and "factor" in rope_scaling:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.YARN)
            self.gguf_writer.add_rope_scaling_factor(rope_scaling["factor"])
            self.gguf_writer.add_rope_scaling_orig_ctx_len(rope_scaling["original_max_position_embeddings"])

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        if name.startswith("model.visual."): # ignore visual part of Glm4v
            return []
        elif name.startswith("model.language_model."):
            name = name.replace("language_model.", "") # for Glm4v
        return super().modify_tensors(data_torch, name, bid)


@ModelBase.register("GlmForCausalLM", "ChatGLMModel", "ChatGLMForConditionalGeneration")
class ChatGLMModel(TextModel):
    model_arch = gguf.MODEL_ARCH.CHATGLM

    def set_vocab_chatglm3(self):
        dir_model = self.dir_model
        hparams = self.hparams
        tokens: list[bytes] = []
        toktypes: list[int] = []
        scores: list[float] = []

        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(dir_model, trust_remote_code=True)
        vocab_size = hparams.get("padded_vocab_size", len(tokenizer.get_vocab()))
        assert max(tokenizer.get_vocab().values()) < vocab_size
        role_special_tokens = ["<|system|>", "<|user|>", "<|assistant|>", "<|observation|>"]
        special_tokens = ["[MASK]", "[gMASK]", "[sMASK]", "sop", "eop"] + role_special_tokens
        for token_id in range(vocab_size):
            piece = tokenizer._convert_id_to_token(token_id)
            if token_id == 0:
                piece = "<unk>"
            elif token_id == 1:
                piece = "<bos>"
            elif token_id == 2:
                piece = "<eos>"

            text = piece.encode("utf-8")
            score = 0.0
            # Referencing the tokenizer Python implementation(https://huggingface.co/THUDM/chatglm3-6b/blob/main/tokenization_chatglm.py),
            # it is only valid if it is less than tokenizer.tokenizer.sp_model.vocab_size()
            if len(piece) != 0 and token_id < tokenizer.tokenizer.sp_model.vocab_size():
                score = tokenizer.tokenizer.sp_model.get_score(token_id)

            if token_id >= tokenizer.tokenizer.sp_model.vocab_size():
                if piece in special_tokens:
                    toktype = SentencePieceTokenTypes.CONTROL
                elif len(piece) == 0:
                    text = f"[PAD{token_id}]".encode("utf-8")
                    toktype = SentencePieceTokenTypes.UNUSED
                else:
                    toktype = SentencePieceTokenTypes.USER_DEFINED
                tokens.append(text)
                scores.append(score)
                toktypes.append(toktype)
                continue

            toktype = SentencePieceTokenTypes.NORMAL
            if tokenizer.tokenizer.sp_model.is_unknown(token_id):
                toktype = SentencePieceTokenTypes.UNKNOWN
            elif tokenizer.tokenizer.sp_model.is_control(token_id):
                toktype = SentencePieceTokenTypes.CONTROL
            elif tokenizer.tokenizer.sp_model.is_unused(token_id):
                toktype = SentencePieceTokenTypes.UNUSED
            elif tokenizer.tokenizer.sp_model.is_byte(token_id):
                toktype = SentencePieceTokenTypes.BYTE

            tokens.append(text)
            scores.append(score)
            toktypes.append(toktype)

        self.gguf_writer.add_tokenizer_model("llama")
        # glm3 needs prefix and suffix formatted as:
        # prompt = "[gMASK]sop<|user|>\n" + prompt + "<|assistant|>"
        self.gguf_writer.add_tokenizer_pre("chatglm-spm")
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_scores(scores)
        self.gguf_writer.add_token_types(toktypes)

        special_vocab = gguf.SpecialVocab(self.dir_model, n_vocab=len(tokens))
        special_vocab.add_to_gguf(self.gguf_writer)

    @staticmethod
    def token_bytes_to_string(b):
        from transformers.models.gpt2.tokenization_gpt2 import bytes_to_unicode
        byte_encoder = bytes_to_unicode()
        return ''.join([byte_encoder[ord(char)] for char in b.decode('latin-1')])

    @staticmethod
    def bpe(mergeable_ranks: dict[bytes, int], token: bytes, max_rank: int | None = None) -> list[bytes]:
        parts = [bytes([b]) for b in token]
        while True:
            min_idx = None
            min_rank = None
            for i, pair in enumerate(zip(parts[:-1], parts[1:])):
                rank = mergeable_ranks.get(pair[0] + pair[1])
                if rank is not None and (min_rank is None or rank < min_rank):
                    min_idx = i
                    min_rank = rank
            if min_rank is None or (max_rank is not None and min_rank >= max_rank):
                break
            assert min_idx is not None
            parts = parts[:min_idx] + [parts[min_idx] + parts[min_idx + 1]] + parts[min_idx + 2:]
        return parts

    def set_vocab(self):
        if "THUDM/chatglm3-6b" in self.hparams.get("_name_or_path", ""):
            self.set_vocab_chatglm3()
            return

        dir_model = self.dir_model
        hparams = self.hparams
        tokens: list[str] = []
        toktypes: list[int] = []

        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(dir_model, trust_remote_code=True)
        vocab_size = hparams.get("padded_vocab_size",hparams["vocab_size"])
        assert max(tokenizer.get_vocab().values()) < vocab_size

        tokens, toktypes, tokpre = self.get_vocab_base()
        self.gguf_writer.add_tokenizer_model("gpt2")
        self.gguf_writer.add_tokenizer_pre(tokpre)
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_types(toktypes)
        special_vocab = gguf.SpecialVocab(self.dir_model, load_merges=True)
        # only add special tokens when they were not already loaded from config.json
        special_vocab._set_special_token("eos", tokenizer.get_added_vocab()["<|endoftext|>"])
        special_vocab._set_special_token("eot", tokenizer.get_added_vocab()["<|user|>"])
        # this one is usually not in config.json anyway
        special_vocab._set_special_token("unk", tokenizer.get_added_vocab()["<|endoftext|>"])
        special_vocab.add_to_gguf(self.gguf_writer)

    def set_gguf_parameters(self):
        n_embed = self.hparams.get("hidden_size", self.hparams.get("n_embed"))
        n_head = self.hparams.get("n_head", self.hparams.get("num_attention_heads"))
        n_head_kv = self.hparams.get("multi_query_group_num", self.hparams.get("num_key_value_heads", n_head))
        self.gguf_writer.add_context_length(self.hparams.get("seq_length", n_embed))
        self.gguf_writer.add_embedding_length(n_embed)
        self.gguf_writer.add_feed_forward_length(self.hparams.get("ffn_hidden_size", self.hparams.get("intermediate_size", 4 * n_embed)))
        self.gguf_writer.add_block_count(self.hparams.get("num_layers", self.hparams["num_hidden_layers"]))
        self.gguf_writer.add_head_count(n_head)
        self.gguf_writer.add_head_count_kv(n_head_kv)
        self.gguf_writer.add_layer_norm_rms_eps(self.hparams.get("layernorm_epsilon",1e-5))
        self.gguf_writer.add_file_type(self.ftype)
        if "attention_dim" in self.hparams:
            rope_dim = self.hparams["attention_dim"]
        else:
            rope_dim = self.hparams["hidden_size"] // self.hparams["num_attention_heads"]
        self.gguf_writer.add_rope_dimension_count(int(rope_dim * self.hparams.get("partial_rotary_factor", 0.5)))
        self.gguf_writer.add_add_bos_token(False)
        rope_freq = 10000
        if "rope_ratio" in self.hparams:
            rope_freq = rope_freq * self.hparams["rope_ratio"]
        self.gguf_writer.add_rope_freq_base(rope_freq)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        if name.endswith(".rotary_pos_emb.inv_freq") or name.startswith("model.vision."):
            return []

        name = name.removeprefix("transformer.")
        return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("NemotronForCausalLM")
class NemotronModel(TextModel):
    model_arch = gguf.MODEL_ARCH.NEMOTRON

    def set_vocab(self):
        self._set_vocab_sentencepiece()
        self.gguf_writer.add_pad_token_id(0)
        self.gguf_writer.add_unk_token_id(1)

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        hparams = self.hparams
        self.gguf_writer.add_vocab_size(hparams["vocab_size"])

        f_norm_eps = self.find_hparam(["layer_norm_eps", "layer_norm_epsilon", "norm_epsilon", "norm_eps"])
        self.gguf_writer.add_layer_norm_eps(f_norm_eps)

        # * Partial RoPE
        rot_pct = self.find_hparam(["partial_rotary_factor", "rope_pct", "rope_percent"])
        n_embd = self.find_hparam(["hidden_size", "n_embd"])
        n_head = self.find_hparam(["num_attention_heads", "n_head"])
        self.gguf_writer.add_rope_dimension_count(int(rot_pct * n_embd) // n_head)

        # * RopeScaling for Nemotron
        if "rope_scaling" not in self.hparams or self.hparams["rope_scaling"] is None:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.NONE)
        else:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.LINEAR)
            self.gguf_writer.add_rope_scaling_factor(self.hparams["factor"])

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        # * Adding +1 to LayerNorm's weights here to implement layernorm1p w/o changing anything on the GGML engine side
        #   model.layers.{l}.input_layernorm.weight
        #   model.layers.{l}.post_attention_layernorm.weight
        #   model.norm.weight
        if name.endswith("norm.weight"):
            data_torch = data_torch + 1

        return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("ExaoneForCausalLM")
class ExaoneModel(TextModel):
    model_arch = gguf.MODEL_ARCH.EXAONE

    def set_gguf_parameters(self):
        hparams = self.hparams

        assert (hparams["activation_function"] == "silu")

        max_position_embeddings = hparams["max_position_embeddings"]
        embed_dim = hparams["hidden_size"]
        num_heads = hparams["num_attention_heads"]
        num_kv_heads = hparams.get("num_key_value_heads", num_heads)
        layer_norm_eps = hparams["layer_norm_epsilon"]
        intermediate_size = hparams["intermediate_size"] if "intermediate_size" in hparams else 4 * embed_dim
        num_layers = hparams["num_layers"]
        # ignore for now as EXAONE-3.0-7.8B-Instruct attentino_dropout is 0.0
        # attention_dropout_rate = hparams["attention_dropout"]
        # ignore for now as EXAONE-3.0-7.8B-Instruct embed_dropout is 0.0
        # embed_dropout_rate = hparams["embed_dropout"]
        self.gguf_writer.add_embedding_length(embed_dim)
        self.gguf_writer.add_head_count(num_heads)
        self.gguf_writer.add_head_count_kv(num_kv_heads)
        self.gguf_writer.add_context_length(max_position_embeddings)
        self.gguf_writer.add_layer_norm_rms_eps(layer_norm_eps)
        self.gguf_writer.add_feed_forward_length(intermediate_size)
        self.gguf_writer.add_block_count(num_layers)
        self.gguf_writer.add_file_type(self.ftype)

        if (rope_theta := self.hparams.get("rope_theta")) is not None:
            self.gguf_writer.add_rope_freq_base(rope_theta)
        rotary_factor = self.find_hparam(["partial_rotary_factor", "rope_pct"], optional=True)
        rotary_factor = rotary_factor if rotary_factor is not None else 1.0
        self.gguf_writer.add_rope_dimension_count(int(rotary_factor * (hparams["hidden_size"] // hparams["num_attention_heads"])))
        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "linear" and "factor" in rope_scaling:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.LINEAR)
            self.gguf_writer.add_rope_scaling_factor(rope_scaling["factor"])

    def generate_extra_tensors(self) -> Iterable[tuple[str, Tensor]]:
        if rope_scaling := self.find_hparam(["rope_scaling"], optional=True):
            if rope_scaling.get("rope_type", '').lower() == "llama3":
                base = self.hparams.get("rope_theta", 10000.0)
                if (dim := self.hparams.get("head_dim")) is None:
                    dim = self.hparams["hidden_size"] // self.hparams["num_attention_heads"]
                freqs = 1.0 / (base ** (torch.arange(0, dim, 2, dtype=torch.float32) / dim))

                factor = rope_scaling.get("factor", 8.0)
                low_freq_factor = rope_scaling.get("low_freq_factor", 1.0)
                high_freq_factor = rope_scaling.get("high_freq_factor", 4.0)
                old_context_len = self.hparams.get("original_max_position_embeddings", 8192)

                low_freq_wavelen = old_context_len / low_freq_factor
                high_freq_wavelen = old_context_len / high_freq_factor
                assert low_freq_wavelen != high_freq_wavelen

                rope_factors = []
                for freq in freqs:
                    wavelen = 2 * math.pi / freq
                    if wavelen < high_freq_wavelen:
                        rope_factors.append(1)
                    elif wavelen > low_freq_wavelen:
                        rope_factors.append(factor)
                    else:
                        smooth = (old_context_len / wavelen - low_freq_factor) / (high_freq_factor - low_freq_factor)
                        rope_factors.append(1 / ((1 - smooth) / factor + smooth))

                yield (self.format_tensor_name(gguf.MODEL_TENSOR.ROPE_FREQS), torch.tensor(rope_factors, dtype=torch.float32))


@ModelBase.register("Exaone4ForCausalLM")
class Exaone4Model(TextModel):
    model_arch = gguf.MODEL_ARCH.EXAONE4

    def set_vocab(self):
        tokens, toktypes, tokpre = self.get_vocab_base()
        self.gguf_writer.add_tokenizer_model("gpt2")
        self.gguf_writer.add_tokenizer_pre(tokpre)
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_types(toktypes)

        special_vocab = gguf.SpecialVocab(self.dir_model, load_merges=True)
        special_vocab.add_to_gguf(self.gguf_writer)

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        hparams = self.hparams
        self.gguf_writer.add_vocab_size(hparams["vocab_size"])

        if hparams.get("sliding_window") is not None:
            self.gguf_writer.add_sliding_window(hparams["sliding_window"])
            if "layer_types" in hparams:
                self.gguf_writer.add_sliding_window_pattern([t == "sliding_attention" for t in hparams["layer_types"]])
            elif "sliding_window_pattern" in hparams:
                sliding_window_pattern = []
                if isinstance(hparams["sliding_window_pattern"], str):  # e.g. LLLG
                    for i in range(hparams["num_hidden_layers"]):
                        sliding_window_pattern.append(hparams["sliding_window_pattern"][i % len(hparams["sliding_window_pattern"])] == "L")
                if isinstance(hparams["sliding_window_pattern"], int):  # e.g. 4
                    for i in range(hparams["num_hidden_layers"]):
                        sliding_window_pattern.append((i + 1) % hparams["sliding_window_pattern"] != 0)
                if len(sliding_window_pattern) == hparams["num_hidden_layers"]:
                    self.gguf_writer.add_sliding_window_pattern(sliding_window_pattern)

        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "linear" and "factor" in rope_scaling:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.LINEAR)
            self.gguf_writer.add_rope_scaling_factor(rope_scaling["factor"])

    def generate_extra_tensors(self) -> Iterable[tuple[str, Tensor]]:
        if rope_scaling := self.find_hparam(["rope_scaling"], optional=True):
            if rope_scaling.get("rope_type", '').lower() == "llama3":
                base = self.hparams.get("rope_theta", 10_000.0)
                if (dim := self.hparams.get("head_dim")) is None:
                    dim = self.hparams["hidden_size"] // self.hparams["num_attention_heads"]
                freqs = 1.0 / (base ** (torch.arange(0, dim, 2, dtype=torch.float32) / dim))

                factor = rope_scaling.get("factor", 16.0)
                low_freq_factor = rope_scaling.get("low_freq_factor", 1.0)
                high_freq_factor = rope_scaling.get("high_freq_factor", 4.0)
                old_context_len = self.hparams.get("original_max_position_embeddings", 8192)

                low_freq_wavelen = old_context_len / low_freq_factor
                high_freq_wavelen = old_context_len / high_freq_factor

                rope_factors = []
                for freq in freqs:
                    wavelen = 2 * math.pi / freq
                    if wavelen < high_freq_wavelen:
                        rope_factors.append(1)
                    elif wavelen > low_freq_wavelen:
                        rope_factors.append(factor)
                    else:
                        smooth = (old_context_len / wavelen - low_freq_factor) / (high_freq_factor - low_freq_factor)
                        rope_factors.append(1 / ((1 - smooth) / factor + smooth))

                yield (self.format_tensor_name(gguf.MODEL_TENSOR.ROPE_FREQS), torch.tensor(rope_factors, dtype=torch.float32))


@ModelBase.register("GraniteForCausalLM")
class GraniteModel(LlamaModel):
    """Conversion for IBM's GraniteForCausalLM"""
    model_arch = gguf.MODEL_ARCH.GRANITE

    def set_gguf_parameters(self):
        """Granite uses standard llama parameters with the following differences:

        - No head_dim support
        - New multiplier params:
            - attention_scale
            - embedding_scale
            - residual_scale
        - logits_scaling
        """
        if head_dim := self.hparams.pop("head_dim", None):
            logger.warning("Ignoring head_dim (%s) from config for Granite", head_dim)
        super().set_gguf_parameters()
        # NOTE: Convert _multiplier params to _scale params for naming
        #   consistency
        if attention_scale := self.hparams.get("attention_multiplier"):
            self.gguf_writer.add_attention_scale(attention_scale)
            logger.info("gguf: (granite) attention_scale = %s", attention_scale)
        if embedding_scale := self.hparams.get("embedding_multiplier"):
            self.gguf_writer.add_embedding_scale(embedding_scale)
            logger.info("gguf: (granite) embedding_scale = %s", embedding_scale)
        if residual_scale := self.hparams.get("residual_multiplier"):
            self.gguf_writer.add_residual_scale(residual_scale)
            logger.info("gguf: (granite) residual_scale = %s", residual_scale)
        if logits_scale := self.hparams.get("logits_scaling"):
            self.gguf_writer.add_logit_scale(logits_scale)
            logger.info("gguf: (granite) logits_scale = %s", logits_scale)


@ModelBase.register("GraniteMoeForCausalLM", "GraniteMoeSharedForCausalLM")
class GraniteMoeModel(GraniteModel):
    """Conversion for IBM's GraniteMoeForCausalLM"""
    model_arch = gguf.MODEL_ARCH.GRANITE_MOE

    def set_gguf_parameters(self):
        """GraniteMoeShared uses GraniteMoe parameters plus the following:
        - shared_intermediate_size
        """
        super().set_gguf_parameters()
        if shared_feed_forward_length := self.hparams.get("shared_intermediate_size"):
            self.gguf_writer.add_expert_shared_feed_forward_length(shared_feed_forward_length)
            logger.info("gguf: (granitemoeshared) shared_feed_forward_length = %s", shared_feed_forward_length)

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        """In modeling_granitemoe, the JetMoe implementation of parallel experts
        is used. This essentially merges w1 and w3 into a single tensor with 2x
        the hidden size that is then split during forward. To keep compatibility
        with existing mixtral support, we pull them apart here.
        """

        if name.endswith("block_sparse_moe.input_linear.weight"):
            ffn_dim = self.hparams["intermediate_size"]
            assert data_torch.shape[-2] == 2 * ffn_dim, "Merged FFN tensor size must be 2 * intermediate_size"
            gate, up = data_torch.split(ffn_dim, dim=-2)
            return [
                (self.format_tensor_name(gguf.MODEL_TENSOR.FFN_GATE_EXP, bid), gate),
                (self.format_tensor_name(gguf.MODEL_TENSOR.FFN_UP_EXP, bid), up),
            ]

        has_experts = bool(self.hparams.get('num_local_experts'))

        if name.endswith("shared_mlp.input_linear.weight"):
            ffn_dim = self.hparams["shared_intermediate_size"]
            assert data_torch.shape[-2] == 2 * ffn_dim, "Merged FFN tensor size must be 2 * shared_intermediate_size"
            gate, up = data_torch.split(ffn_dim, dim=-2)
            if has_experts:
                return [
                    (self.format_tensor_name(gguf.MODEL_TENSOR.FFN_GATE_SHEXP, bid), gate),
                    (self.format_tensor_name(gguf.MODEL_TENSOR.FFN_UP_SHEXP, bid), up),
                ]
            return [
                (self.format_tensor_name(gguf.MODEL_TENSOR.FFN_GATE, bid), gate),
                (self.format_tensor_name(gguf.MODEL_TENSOR.FFN_UP, bid), up),
            ]

        if not has_experts and name.endswith("shared_mlp.output_linear.weight"):
            return [
                (self.format_tensor_name(gguf.MODEL_TENSOR.FFN_DOWN, bid), data_torch)
            ]

        return super().modify_tensors(data_torch, name, bid)


@ModelBase.register("GraniteMoeHybridForCausalLM", "BambaForCausalLM")
class GraniteHybridModel(Mamba2Model, GraniteMoeModel):
    """GraniteHybrid is a hybrid SSM + Attention model that uses Mamba2 SSM
    layers and optionally uses MoE w/ a shared expert"""
    model_arch = gguf.MODEL_ARCH.GRANITE_HYBRID
    undo_permute = True

    def __init__(self, *args, **kwargs):

        # Hybrid mamba models use a prefix for the mamba-specific params.
        # TODO: Extend this if the prefix(es) need to be configurable
        self.hparam_prefixes = ["mamba"]

        super().__init__(*args, **kwargs)

        # Lists of which layers use ssm vs attention
        self._attn_layers = self.get_attn_layers()
        self._ssm_layers = [
            i for i in range(self.block_count)
            if i not in self._attn_layers
        ]

        # n_group and d_inner are used during reshape_tensors for mamba2
        self.d_model = self.find_hparam(["hidden_size", "d_model"])
        self.n_group = self.find_hparam(["n_groups"])
        self.d_inner = self.find_hparam(["expand"]) * self.d_model

    def get_attn_layers(self):
        # Explicit list of layer type names
        if layer_types := self.hparams.get("layer_types"):
            return [
                i for i, typ in enumerate(layer_types)
                if typ == "attention"
            ]

        # Layer types indicated by index or period
        attn_layers = self.hparams.get("attn_layer_indices", [])
        if not attn_layers:
            attn_period = self.hparams.get("attn_layer_period")
            assert attn_period, "Didn't find attn_layer_indices or attn_layer_period"
            attn_offset = self.hparams.get("attn_layer_offset")
            assert attn_offset is not None, "No attention layer offset set with attn_layer_period"
            attn_layers = [
                i for i in range(self.block_count)
                if i % attn_period == attn_offset
            ]
        return attn_layers

    def find_hparam(self, keys: Iterable[str], *args, **kwargs) -> Any:
        prefixed = []
        for pfx in self.hparam_prefixes:
            prefixed.extend(
                "_".join([pfx, k])
                for k in keys
            )
        keys = list(keys) + prefixed
        return Mamba2Model.find_hparam(self, keys, *args, **kwargs)

    def modify_tensors(
        self, data_torch: Tensor, name: str, bid: int | None
    ) -> Iterable[tuple[str, Tensor]]:
        if (
            name.endswith("block_sparse_moe.input_linear.weight")
            or "shared_mlp" in name
        ):
            return GraniteMoeModel.modify_tensors(self, data_torch, name, bid)

        # Determine whether this is a mamba layer or an attention layer
        if bid in self._ssm_layers:
            return Mamba2Model.modify_tensors(self, data_torch, name, bid)
        elif bid in self._attn_layers:
            return GraniteMoeModel.modify_tensors(self, data_torch, name, bid)
        return [(self.map_tensor_name(name), data_torch)]

    def set_gguf_parameters(self):
        """This method merges params from both parents and some that are
        specific to this model. The result is some duplication of how the params
        get set. The following warnings are expected during conversion:

        WARNING:Duplicated key name 'granitehybrid.attention.head_count_kv'
        WARNING:Duplicated key name 'granitehybrid.context_length'
        """
        GraniteMoeModel.set_gguf_parameters(self)

        ## Mamba mixer params ##
        self.gguf_writer.add_ssm_conv_kernel(self.find_hparam(["conv_kernel", "d_conv"]))
        self.gguf_writer.add_ssm_state_size(self.find_hparam(["state_size", "d_state"]))
        self.gguf_writer.add_ssm_group_count(self.n_group)
        self.gguf_writer.add_ssm_inner_size(self.d_inner)
        # NOTE: The mamba_dt_rank is _not_ the right field for how this is used
        #   in llama.cpp
        self.gguf_writer.add_ssm_time_step_rank(self.find_hparam(["n_heads"]))

        ## Attention params ##
        head_count_kv = self.find_hparam(["num_key_value_heads", "n_head_kv"])
        head_count_kv_vec = [
            head_count_kv if i in self._attn_layers else 0 for i in range(self.block_count)
        ]
        if rope_dim := self.hparams.get("attn_rotary_emb"):
            self.gguf_writer.add_rope_dimension_count(rope_dim)
        self.gguf_writer.add_head_count_kv(head_count_kv_vec)

        ## If Bamba, use rope, otherwise don't
        use_rope = "BambaForCausalLM" in self.hparams["architectures"]
        self.gguf_writer.add_rope_scaling_finetuned(use_rope)
        if not use_rope:
            self.gguf_writer.add_context_length(2**20)

        ## Validation ##
        d_head = self.find_hparam(["d_head"], optional=True) or 64
        assert self.hparams.get("hidden_act") in [None, "silu"], "Only SILU activation supported"
        assert self.d_inner % d_head == 0, f"SSM inner size {self.d_inner} not a multiple of head dim {d_head}"

    def set_vocab(self):
        self.hparams["pad_vocab_size_multiple"] = 8
        Mamba2Model.set_vocab(self)


@ModelBase.register("BailingMoeForCausalLM")
class BailingMoeModel(TextModel):
    model_arch = gguf.MODEL_ARCH.BAILINGMOE

    def set_vocab(self):
        self._set_vocab_gpt2()

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        hparams = self.hparams
        if (rope_dim := hparams.get("head_dim")) is None:
            rope_dim = hparams["hidden_size"] // hparams["num_attention_heads"]

        self.gguf_writer.add_rope_dimension_count(rope_dim)
        rope_scaling = self.hparams.get("rope_scaling") or {}
        if rope_scaling.get("rope_type", rope_scaling.get("type")) == "yarn" and "factor" in rope_scaling:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.YARN)
            self.gguf_writer.add_rope_scaling_factor(rope_scaling["factor"])
            self.gguf_writer.add_rope_scaling_orig_ctx_len(rope_scaling["original_max_position_embeddings"])
        else:
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.NONE)
        self.gguf_writer.add_leading_dense_block_count(hparams["first_k_dense_replace"])
        self.gguf_writer.add_vocab_size(hparams["vocab_size"])
        self.gguf_writer.add_expert_feed_forward_length(hparams["moe_intermediate_size"])
        self.gguf_writer.add_expert_weights_scale(1.0)
        self.gguf_writer.add_expert_count(hparams["num_experts"])
        self.gguf_writer.add_expert_shared_count(hparams["num_shared_experts"])
        self.gguf_writer.add_expert_weights_norm(hparams["norm_topk_prob"])

    _experts: list[dict[str, Tensor]] | None = None

    @staticmethod
    def permute(weights: Tensor, n_head: int, n_head_kv: int | None):
        if n_head_kv is not None and n_head != n_head_kv:
            n_head = n_head_kv
        return (weights.reshape(n_head, 2, weights.shape[0] // n_head // 2, *weights.shape[1:])
                .swapaxes(1, 2)
                .reshape(weights.shape))

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        n_head = self.hparams["num_attention_heads"]
        n_kv_head = self.hparams.get("num_key_value_heads")
        n_embd = self.hparams["hidden_size"]
        if (head_dim := self.hparams.get("head_dim")) is None:
            head_dim = n_embd // n_head

        output_name = self.format_tensor_name(gguf.MODEL_TENSOR.OUTPUT)

        if name.endswith("attention.dense.weight"):
            return [(self.format_tensor_name(gguf.MODEL_TENSOR.ATTN_OUT, bid), data_torch)]
        elif name.endswith("query_key_value.weight"):
            q, k, v = data_torch.split([n_head * head_dim, n_kv_head * head_dim, n_kv_head * head_dim], dim=-2)

            return [
                (self.format_tensor_name(gguf.MODEL_TENSOR.ATTN_Q, bid), BailingMoeModel.permute(q, n_head, n_head)),
                (self.format_tensor_name(gguf.MODEL_TENSOR.ATTN_K, bid), BailingMoeModel.permute(k, n_head, n_kv_head)),
                (self.format_tensor_name(gguf.MODEL_TENSOR.ATTN_V, bid), v)
            ]
        elif name.find("mlp.experts") != -1:
            n_experts = self.hparams["num_experts"]
            assert bid is not None

            tensors: list[tuple[str, Tensor]] = []

            if self._experts is None:
                self._experts = [{} for _ in range(self.block_count)]

            self._experts[bid][name] = data_torch

            if len(self._experts[bid]) >= n_experts * 3:
                # merge the experts into a single 3d tensor
                for w_name in ["down_proj", "gate_proj", "up_proj"]:
                    datas: list[Tensor] = []

                    for xid in range(n_experts):
                        ename = f"model.layers.{bid}.mlp.experts.{xid}.{w_name}.weight"
                        datas.append(self._experts[bid][ename])
                        del self._experts[bid][ename]

                    data_torch = torch.stack(datas, dim=0)

                    merged_name = f"model.layers.{bid}.mlp.experts.{w_name}.weight"

                    new_name = self.map_tensor_name(merged_name)

                    tensors.append((new_name, data_torch))

            return tensors

        new_name = self.map_tensor_name(name)

        if new_name == output_name and self.hparams.get("norm_head"):
            data_torch = data_torch.float()
            data_torch /= torch.norm(data_torch, p=2, dim=0, keepdim=True) + 1e-7

        return [(new_name, data_torch)]

    def prepare_tensors(self):
        super().prepare_tensors()

        if self._experts is not None:
            # flatten `list[dict[str, Tensor]]` into `list[str]`
            experts = [k for d in self._experts for k in d.keys()]
            if len(experts) > 0:
                raise ValueError(f"Unprocessed experts: {experts}")


@ModelBase.register("ChameleonForConditionalGeneration")
@ModelBase.register("ChameleonForCausalLM")  # obsolete
class ChameleonModel(TextModel):
    model_arch = gguf.MODEL_ARCH.CHAMELEON

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_swin_norm(self.hparams.get("swin_norm", False))

    def set_vocab(self):
        self._set_vocab_gpt2()

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        # ignore image tokenizer for now
        # TODO: remove this once image support is implemented for Chameleon
        if name.startswith("model.vqmodel"):
            return []

        n_head = self.hparams["num_attention_heads"]
        n_kv_head = self.hparams.get("num_key_value_heads")
        hidden_dim = self.hparams.get("hidden_size")

        if name.endswith(("q_proj.weight", "q_proj.bias")):
            data_torch = LlamaModel.permute(data_torch, n_head, n_head)
        if name.endswith(("k_proj.weight", "k_proj.bias")):
            data_torch = LlamaModel.permute(data_torch, n_head, n_kv_head)
        if name.endswith(("q_norm.weight", "q_norm.bias")):
            data_torch = ChameleonModel._reverse_hf_permute(data_torch, n_head, hidden_dim)
        if name.endswith(("k_norm.weight", "k_norm.bias")):
            data_torch = ChameleonModel._reverse_hf_permute(data_torch, n_kv_head, hidden_dim)

        return [(self.map_tensor_name(name), data_torch)]

    # see: https://github.com/huggingface/transformers/blob/72fb02c47dbbe1999ae105319f24631cad6e2e00/src/transformers/models/chameleon/convert_chameleon_weights_to_hf.py#L176-L203
    @staticmethod
    def _reverse_hf_permute(data_torch, n_heads, hidden_dim):
        head_dim = hidden_dim // n_heads
        data_torch = data_torch[0].view(2, head_dim // 2).t().reshape(1, -1)
        data_torch = data_torch.repeat_interleave(n_heads, 0)
        return data_torch


@ModelBase.register("UltravoxModel")
class UltravoxModel(TextModel):
    model_arch = gguf.MODEL_ARCH.LLAMA # dummy

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        raise NotImplementedError("Ultravox does not have text decoder. Instead, it uses Llama or other models for text. If you want to get the audio encoder, please use --mmproj argument")


@ModelBase.register("Qwen2AudioForConditionalGeneration")
class WhisperEncoderModel(MmprojModel):
    has_vision_encoder = False # no vision encoder
    has_audio_encoder = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hparams["hidden_size"] = self.hparams["d_model"]
        self.hparams["intermediate_size"] = self.hparams["encoder_ffn_dim"]
        self.hparams["num_attention_heads"] = self.hparams["encoder_attention_heads"]

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_clip_projector_type(gguf.VisionProjectorType.QWEN2A)
        self.gguf_writer.add_audio_num_mel_bins(self.hparams["num_mel_bins"])
        self.gguf_writer.add_audio_attention_layernorm_eps(self.hparams.get("layer_norm_eps", 1e-5))

    def tensor_force_quant(self, name, new_name, bid, n_dims):
        del bid, new_name, n_dims  # unused
        if ".conv" in name and ".weight" in name:
            return gguf.GGMLQuantizationType.F16
        return False

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        del bid  # unused

        if name.startswith("language_model."):
            # skip language model tensors
            return []

        # prevent clash naming with vision tensors
        if name.startswith("multi_modal_projector"):
            name = "audio." + name

        if "conv1.bias" in name or "conv2.bias" in name:
            # transpose conv1 and conv2 bias
            data_torch = data_torch.unsqueeze(-1)

        return [(self.map_tensor_name(name), data_torch)]


@ModelBase.register("UltravoxModel")
class UltravoxWhisperEncoderModel(WhisperEncoderModel):
    has_vision_encoder = False # no vision encoder
    has_audio_encoder = True

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        self.gguf_writer.add_audio_stack_factor(self.global_config["stack_factor"])


@ModelBase.register("FalconH1ForCausalLM")
class FalconH1Model(Mamba2Model):
    model_arch = gguf.MODEL_ARCH.FALCON_H1

    def __init__(self, *args, **kwargs):
        # Set the hparam prefixes for Falcon Mamba2
        self.hparam_prefixes = ["mamba"]

        # Initialize the base Mamba2Model
        super().__init__(*args, **kwargs)

        # Use Llama conversion for attention
        self._transformer_model_class = LlamaModel

        # n_group and d_inner are used during reshape_tensors for mamba2
        self.n_group = self.find_hparam(["n_groups"])
        self.d_inner = self.find_hparam(["mamba_d_ssm"])
        self.d_head = self.find_hparam(["d_head"])

        # Initialize any Falcon Mamba2 specific attributes
        self.has_attention = True  # Falcon Mamba2 has attention components

        # Load Falcon-H1 multipliers from hyperparameters
        self.attention_in_multiplier = self.find_hparam(["attention_in_multiplier"], optional=True)
        self.attention_out_multiplier = self.find_hparam(["attention_out_multiplier"], optional=True)
        self.ssm_in_multiplier = self.find_hparam(["ssm_in_multiplier"], optional=True)
        self.ssm_out_multiplier = self.find_hparam(["ssm_out_multiplier"], optional=True)
        self.mlp_multipliers = self.find_hparam(["mlp_multipliers"], optional=True)
        self.ssm_multipliers = self.find_hparam(["ssm_multipliers"], optional=True)
        self.intermediate_size = self.find_hparam(["intermediate_size"])
        self.key_multiplier = self.find_hparam(["key_multiplier"], optional=True)

    def find_hparam(self, keys: Iterable[str], *args, **kwargs) -> Any:
        prefixed = []
        for pfx in self.hparam_prefixes:
            prefixed.extend(
                "_".join([pfx, k])
                for k in keys
            )
        keys = list(keys) + prefixed
        return super().find_hparam(keys, *args, **kwargs)

    def set_vocab(self):
        self._set_vocab_gpt2()

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        tensors = list(super().modify_tensors(data_torch, name, bid))
        tensor = tensors[0][1]

        if "down_proj" in name:
            tensor = tensor  * self.mlp_multipliers[1]
        elif "gate_proj" in name:
            tensor = tensor * self.mlp_multipliers[0]
        elif "k_proj" in name:
            tensor = tensor * self.key_multiplier * self.attention_in_multiplier
        elif "q_proj" in name:
            tensor = tensor * self.attention_in_multiplier
        elif "v_proj" in name:
            tensor = tensor * self.attention_in_multiplier
        elif "o_proj" in name:
            tensor = tensor * self.attention_out_multiplier
        elif "out_proj" in name:
            tensor = tensor * self.ssm_out_multiplier
        elif "in_proj" in name:
            tensor = tensor * self.ssm_in_multiplier
            zxbcdt_multipliers = self.hparams["ssm_multipliers"]
            intermediate_size = self.hparams["mamba_d_ssm"]
            groups_time_state_size = self.hparams["mamba_n_groups"] * self.hparams["mamba_d_state"]
            tensor[:intermediate_size, :] *= zxbcdt_multipliers[0]
            tensor[intermediate_size:2 * intermediate_size, :] *= zxbcdt_multipliers[1]
            tensor[2 * intermediate_size:2 * intermediate_size + groups_time_state_size, :] *= zxbcdt_multipliers[2]
            tensor[2 * intermediate_size + groups_time_state_size:2 * intermediate_size + 2 * groups_time_state_size, :] *= zxbcdt_multipliers[3]
            tensor[2 * intermediate_size + 2 * groups_time_state_size:, :] *= zxbcdt_multipliers[4]
        elif "lm_head" in name:
            tensor = tensor * self.hparams["lm_head_multiplier"]
        elif "embed_tokens" in name:
            tensor = tensor * self.hparams["embedding_multiplier"]
        elif "mamba.norm" in name:
            tensor = tensor.reshape(self.n_group, self.d_inner // self.n_group)

        tensors = [(tensors[0][0], tensor)]
        return tensors

    def set_gguf_parameters(self):
        super().set_gguf_parameters()

        ## General Params ##
        self.gguf_writer.add_vocab_size(self.hparams["vocab_size"])
        # Override some Mamba2 defaults
        self.gguf_writer.add_block_count(self.block_count)
        self.gguf_writer.add_context_length(self.hparams.get("max_position_embeddings", 0))
        self.gguf_writer.add_feed_forward_length(self.hparams["intermediate_size"])

        ## Attention params ##
        self.gguf_writer.add_head_count(self.hparams["num_attention_heads"]) # Override value 0 from Mamba2
        self.gguf_writer.add_head_count_kv(self.hparams["num_key_value_heads"])
        self.gguf_writer.add_key_length(self.hparams["head_dim"])
        self.gguf_writer.add_value_length(self.hparams["head_dim"])

        ## Validation ##
        assert self.hparams.get("hidden_act") in [None, "silu"], "Only SILU activation supported"
        assert self.d_inner % self.d_head == 0, f"SSM inner size {self.d_inner} not a multiple of head dim {self.d_head}"

        # Add any other Falcon Mamba2 specific configuration
        self.gguf_writer.add_rope_freq_base(self.find_hparam(["rope_theta"]))


@ModelBase.register("HunYuanMoEV1ForCausalLM")
class HunYuanMoEModel(TextModel):
    model_arch = gguf.MODEL_ARCH.HUNYUAN_MOE

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # For handling tied embeddings
        self._tok_embd = None

    def set_vocab(self):
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(self.dir_model, trust_remote_code=True)

        # 1. Get the pre-tokenizer identifier hash
        tokpre = self.get_vocab_base_pre(tokenizer)

        # 2. Reverse-engineer the merges list from mergeable_ranks
        merges = []
        vocab = {}
        mergeable_ranks = tokenizer.mergeable_ranks
        for token, rank in mergeable_ranks.items():
            vocab[QwenModel.token_bytes_to_string(token)] = rank
            if len(token) == 1:
                continue
            merged = QwenModel.bpe(mergeable_ranks, token, max_rank=rank)
            if len(merged) == 2: # todo this is an assert in Qwen, why?
                merges.append(' '.join(map(QwenModel.token_bytes_to_string, merged)))

        # 3. Generate the tokens and toktypes lists
        vocab_size = self.hparams["vocab_size"]
        assert tokenizer.vocab_size == vocab_size
        special_tokens = tokenizer.special_tokens
        reverse_vocab = {id_ : encoded_tok for encoded_tok, id_ in {**vocab, **special_tokens}.items()}
        tokens: list[str] = []
        toktypes: list[int] = []
        for i in range(vocab_size):
            if i not in reverse_vocab:
                tokens.append(f"[PAD{i}]")
                toktypes.append(gguf.TokenType.UNUSED)
            else:
                token = reverse_vocab[i]
                tokens.append(token)
                if i in special_tokens.values():
                    toktypes.append(gguf.TokenType.CONTROL)
                else:
                    toktypes.append(gguf.TokenType.NORMAL)

        # 4. Write all vocab-related fields to the GGUF writer
        self.gguf_writer.add_tokenizer_model("gpt2")
        self.gguf_writer.add_tokenizer_pre(tokpre)
        self.gguf_writer.add_token_list(tokens)
        self.gguf_writer.add_token_types(toktypes)
        self.gguf_writer.add_token_merges(merges)

        # 5. Add special tokens and chat templates
        special_vocab = gguf.SpecialVocab(self.dir_model, load_merges=False)
        special_vocab.add_to_gguf(self.gguf_writer)
        # FIX for BOS token: Overwrite incorrect id read from config.json
        self.gguf_writer.add_bos_token_id(127959) # <|bos|>

    def set_gguf_parameters(self):
        super().set_gguf_parameters()
        hparams = self.hparams

        self.gguf_writer.add_expert_count(hparams["num_experts"])
        self.gguf_writer.add_expert_shared_feed_forward_length(hparams["intermediate_size"])

        moe_intermediate_size = hparams["moe_intermediate_size"]
        assert all(n == moe_intermediate_size[0] for n in moe_intermediate_size)
        self.gguf_writer.add_expert_feed_forward_length(moe_intermediate_size[0])

        moe_topk = hparams["moe_topk"]
        assert all(topk == moe_topk[0] for topk in moe_topk)
        self.gguf_writer.add_expert_used_count(moe_topk[0])

        moe_shared_expert = hparams["num_shared_expert"]
        assert all(n == moe_shared_expert[0] for n in moe_shared_expert)
        self.gguf_writer.add_expert_shared_count(moe_shared_expert[0])

        # Rope
        rope_scaling = hparams.get("rope_scaling", {})
        if rope_scaling.get("type") == "dynamic":
            # HunYuan uses NTK Aware Alpha based scaling. Original implementation: https://www.reddit.com/r/LocalLLaMA/comments/14lz7j5/ntkaware_scaled_rope_allows_llama_models_to_have/
            # 1000 corresponds to a usable context length of 256k (https://github.com/Tencent-Hunyuan/Hunyuan-A13B/blob/main/report/Hunyuan_A13B_Technical_Report.pdf)
            alpha = rope_scaling.get("alpha", 1000)
            base = hparams.get("rope_theta", 10000.0)
            dim = (hparams["hidden_size"] // hparams["num_attention_heads"]) # 128
            scaled_base = base * (alpha ** (dim / (dim - 2))) # 10000 * (1000 ** (128 / 126)) = 11158839.9251
            self.gguf_writer.add_rope_freq_base(scaled_base)
            self.gguf_writer.add_rope_scaling_type(gguf.RopeScalingType.NONE)
            self.gguf_writer.add_rope_scaling_factor(1)
            # There is no consistent way to calculate ctx from alpha, and the config is incorrectly set to 32k
            self.gguf_writer.add_rope_scaling_orig_ctx_len(256 * 1024) # 256k context length
            self.gguf_writer.add_context_length(256 * 1024) # 256k context length

            # if any of our assumptions about the values are wrong, something has changed and this may need to be updated
            assert alpha == 1000 and base == 10000.0 and dim == 128 and self.hparams["max_position_embeddings"] in [32 * 1024, 256 * 1024] , \
                "HunYuan dynamic RoPE scaling assumptions changed, please update the logic or context length manually"

    _experts: list[dict[str, Tensor]] | None = None

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        if name == "model.embed_tokens.weight":
            self._tok_embd = data_torch.clone()

        if name == "lm_head.weight":
            if self.hparams.get("tie_word_embeddings", False):
                logger.info("Skipping tied output layer 'lm_head.weight'")
                return []

        if name.find("mlp.experts") != -1:
            n_experts = self.hparams["num_experts"]
            assert bid is not None

            if self._experts is None:
                self._experts = [{} for _ in range(self.block_count)]

            self._experts[bid][name] = data_torch

            if len(self._experts[bid]) >= n_experts * 3:
                # merge the experts into a single 3d tensor
                tensors: list[tuple[str, Tensor]] = []
                for w_name in ["down_proj", "gate_proj", "up_proj"]:
                    datas: list[Tensor] = []

                    for xid in range(n_experts):
                        ename = f"model.layers.{bid}.mlp.experts.{xid}.{w_name}.weight"
                        datas.append(self._experts[bid][ename])
                        del self._experts[bid][ename]

                    data_torch = torch.stack(datas, dim=0)
                    merged_name = f"model.layers.{bid}.mlp.experts.{w_name}.weight"
                    new_name = self.map_tensor_name(merged_name)
                    tensors.append((new_name, data_torch))

                return tensors
            else:
                return []

        return [(self.map_tensor_name(name), data_torch)]

    def prepare_tensors(self):
        super().prepare_tensors()
        if self._experts is not None:
            experts = [k for d in self._experts for k in d.keys()]
            if len(experts) > 0:
                raise ValueError(f"Unprocessed experts: {experts}")


@ModelBase.register("SmolLM3ForCausalLM")
class SmolLM3Model(LlamaModel):
    model_arch = gguf.MODEL_ARCH.SMOLLM3

    def set_vocab(self):
        super().set_vocab()
        # remove unsupported array slicing in chat template
        # ref: https://huggingface.co/ggml-org/SmolLM3-3B-GGUF/discussions/1
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(self.dir_model)
        if tokenizer.chat_template is not None:
            chat_template = tokenizer.chat_template.replace("[:]", "")
            self.gguf_writer.add_chat_template(chat_template)


@ModelBase.register("Lfm2ForCausalLM")
@ModelBase.register("LFM2ForCausalLM")
class LFM2Model(TextModel):
    model_arch = gguf.MODEL_ARCH.LFM2

    def _add_feed_forward_length(self):
        ff_dim = self.hparams["block_ff_dim"]

        auto_adjust_ff_dim = self.hparams["block_auto_adjust_ff_dim"]
        ff_dim = self.hparams["block_ff_dim"]
        ffn_dim_multiplier = self.hparams["block_ffn_dim_multiplier"]
        multiple_of = self.hparams["block_multiple_of"]

        if auto_adjust_ff_dim:
            ff_dim = int(2 * ff_dim / 3)
            # custom dim factor multiplier
            if ffn_dim_multiplier is not None:
                ff_dim = int(ffn_dim_multiplier * ff_dim)
            ff_dim = multiple_of * ((ff_dim + multiple_of - 1) // multiple_of)

        self.gguf_writer.add_feed_forward_length(ff_dim)

    def set_gguf_parameters(self):
        # set num_key_value_heads only for attention layers
        self.hparams["num_key_value_heads"] = [
            self.hparams["num_key_value_heads"] if layer_type == "full_attention" else 0
            for layer_type in self.hparams["layer_types"]
        ]

        super().set_gguf_parameters()
        self.gguf_writer.add_vocab_size(self.hparams["vocab_size"])
        self.gguf_writer.add_shortconv_l_cache(self.hparams["conv_L_cache"])
        self.gguf_writer.add_layer_norm_rms_eps(self.hparams["norm_eps"])
        self._add_feed_forward_length()

    def modify_tensors(self, data_torch: Tensor, name: str, bid: int | None) -> Iterable[tuple[str, Tensor]]:
        # conv op requires 2d tensor
        if 'conv.conv' in name:
            data_torch = data_torch.squeeze(1)

        return [(self.map_tensor_name(name), data_torch)]


###### CONVERSION LOGIC ######


# tree of lazy tensors
class LazyTorchTensor(gguf.LazyBase):
    _tensor_type = torch.Tensor
    # to keep the type-checker happy
    dtype: torch.dtype
    shape: torch.Size

    # only used when converting a torch.Tensor to a np.ndarray
    _dtype_map: dict[torch.dtype, type] = {
        torch.float16: np.float16,
        torch.float32: np.float32,
    }

    # used for safetensors slices
    # ref: https://github.com/huggingface/safetensors/blob/079781fd0dc455ba0fe851e2b4507c33d0c0d407/bindings/python/src/lib.rs#L1046
    # TODO: uncomment U64, U32, and U16, ref: https://github.com/pytorch/pytorch/issues/58734
    _dtype_str_map: dict[str, torch.dtype] = {
        "F64": torch.float64,
        "F32": torch.float32,
        "BF16": torch.bfloat16,
        "F16": torch.float16,
        # "U64": torch.uint64,
        "I64": torch.int64,
        # "U32": torch.uint32,
        "I32": torch.int32,
        # "U16": torch.uint16,
        "I16": torch.int16,
        "U8": torch.uint8,
        "I8": torch.int8,
        "BOOL": torch.bool,
        "F8_E4M3": torch.float8_e4m3fn,
        "F8_E5M2": torch.float8_e5m2,
    }

    def numpy(self) -> gguf.LazyNumpyTensor:
        dtype = self._dtype_map[self.dtype]
        return gguf.LazyNumpyTensor(
            meta=gguf.LazyNumpyTensor.meta_with_dtype_and_shape(dtype, self.shape),
            args=(self,),
            func=(lambda s: s.numpy())
        )

    @classmethod
    def meta_with_dtype_and_shape(cls, dtype: torch.dtype, shape: tuple[int, ...]) -> Tensor:
        return torch.empty(size=shape, dtype=dtype, device="meta")

    @classmethod
    def from_safetensors_slice(cls, st_slice: Any) -> Tensor:
        dtype = cls._dtype_str_map[st_slice.get_dtype()]
        shape: tuple[int, ...] = tuple(st_slice.get_shape())
        lazy = cls(meta=cls.meta_with_dtype_and_shape(dtype, shape), args=(st_slice,), func=lambda s: s[:])
        return cast(torch.Tensor, lazy)

    @classmethod
    def from_remote_tensor(cls, remote_tensor: gguf.utility.RemoteTensor):
        dtype = cls._dtype_str_map[remote_tensor.dtype]
        shape = remote_tensor.shape
        meta = cls.meta_with_dtype_and_shape(dtype, shape)
        lazy = cls(meta=meta, args=(remote_tensor,), func=lambda r: torch.frombuffer(r.data(), dtype=dtype).reshape(shape))
        return cast(torch.Tensor, lazy)

    @classmethod
    def __torch_function__(cls, func, types, args=(), kwargs=None):
        del types  # unused

        if kwargs is None:
            kwargs = {}

        if func is torch.Tensor.numpy:
            return args[0].numpy()

        return cls._wrap_fn(func)(*args, **kwargs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a huggingface model to a GGML compatible file")
    parser.add_argument(
        "--vocab-only", action="store_true",
        help="extract only the vocab",
    )
    parser.add_argument(
        "--outfile", type=Path,
        help="path to write to; default: based on input. {ftype} will be replaced by the outtype.",
    )
    parser.add_argument(
        "--outtype", type=str, choices=["f32", "f16", "bf16", "q8_0", "tq1_0", "tq2_0", "auto"], default="f16",
        help="output format - use f32 for float32, f16 for float16, bf16 for bfloat16, q8_0 for Q8_0, tq1_0 or tq2_0 for ternary, and auto for the highest-fidelity 16-bit float type depending on the first loaded tensor type",
    )
    parser.add_argument(
        "--bigendian", action="store_true",
        help="model is executed on big endian machine",
    )
    parser.add_argument(
        "model", type=str,
        help="directory containing model file or huggingface repository ID (if --remote)",
        nargs="?",
    )
    parser.add_argument(
        "--use-temp-file", action="store_true",
        help="use the tempfile library while processing (helpful when running out of memory, process killed)",
    )
    parser.add_argument(
        "--no-lazy", action="store_true",
        help="use more RAM by computing all outputs before writing (use in case lazy evaluation is broken)",
    )
    parser.add_argument(
        "--model-name", type=str, default=None,
        help="name of the model",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="increase output verbosity",
    )
    parser.add_argument(
        "--split-max-tensors", type=int, default=0,
        help="max tensors in each split",
    )
    parser.add_argument(
        "--split-max-size", type=str, default="0",
        help="max size per split N(M|G)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="only print out a split plan and exit, without writing any new files",
    )
    parser.add_argument(
        "--no-tensor-first-split", action="store_true",
        help="do not add tensors to the first split (disabled by default)"
    )
    parser.add_argument(
        "--metadata", type=Path,
        help="Specify the path for an authorship metadata override file"
    )
    parser.add_argument(
        "--print-supported-models", action="store_true",
        help="Print the supported models"
    )
    parser.add_argument(
        "--remote", action="store_true",
        help="(Experimental) Read safetensors file remotely without downloading to disk. Config and tokenizer files will still be downloaded. To use this feature, you need to specify Hugging Face model repo name instead of a local directory. For example: 'HuggingFaceTB/SmolLM2-1.7B-Instruct'. Note: To access gated repo, set HF_TOKEN environment variable to your Hugging Face token.",
    )
    parser.add_argument(
        "--mmproj", action="store_true",
        help="(Experimental) Export multimodal projector (mmproj) for vision models. This will only work on some vision models. A prefix 'mmproj-' will be added to the output file name.",
    )

    args = parser.parse_args()
    if not args.print_supported_models and args.model is None:
        parser.error("the following arguments are required: model")
    return args


def split_str_to_n_bytes(split_str: str) -> int:
    if split_str.endswith("K"):
        n = int(split_str[:-1]) * 1000
    elif split_str.endswith("M"):
        n = int(split_str[:-1]) * 1000 * 1000
    elif split_str.endswith("G"):
        n = int(split_str[:-1]) * 1000 * 1000 * 1000
    elif split_str.isnumeric():
        n = int(split_str)
    else:
        raise ValueError(f"Invalid split size: {split_str}, must be a number, optionally followed by K, M, or G")

    if n < 0:
        raise ValueError(f"Invalid split size: {split_str}, must be positive")

    return n


def get_model_architecture(hparams: dict[str, Any], model_type: ModelType) -> str:
    # TODO @ngxson : this won't work correctly if the model has both audio & vision encoders
    # maybe we should fallback to text model's arch in that case, since not many models have both
    text_config = hparams.get("text_config", {})
    vision_config = hparams.get("vision_config", {})
    arch = None
    if (arches := hparams.get("architectures")) is not None and len(arches) > 0:
        arch = arches[0]
    elif "ssm_cfg" in hparams:
        # For non-hf Mamba and Mamba2 models
        arch = hparams["ssm_cfg"].get("layer", "Mamba") + "ForCausalLM"

    # if "architectures" is found in the sub-config, use that instead
    if model_type == ModelType.TEXT and text_config.get("architectures") is not None:
        arch = text_config["architectures"][0]
    elif model_type == ModelType.MMPROJ and vision_config.get("architectures") is not None:
        arch = vision_config["architectures"][0]
    if arch is None:
        raise ValueError("Failed to detect model architecture")
    return arch


def main() -> None:
    args = parse_args()

    if args.print_supported_models:
        logger.error("Supported models:")
        ModelBase.print_registered_models()
        sys.exit(0)

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    if args.remote:
        hf_repo_id = args.model
        from huggingface_hub import snapshot_download
        local_dir = snapshot_download(
            repo_id=hf_repo_id,
            allow_patterns=["LICENSE", "*.json", "*.md", "*.txt", "tokenizer.model"])
        dir_model = Path(local_dir)
        logger.info(f"Downloaded config and tokenizer to {local_dir}")
    else:
        hf_repo_id = None
        dir_model = Path(args.model)

    if not dir_model.is_dir():
        logger.error(f'Error: {dir_model} is not a directory')
        sys.exit(1)

    ftype_map: dict[str, gguf.LlamaFileType] = {
        "f32": gguf.LlamaFileType.ALL_F32,
        "f16": gguf.LlamaFileType.MOSTLY_F16,
        "bf16": gguf.LlamaFileType.MOSTLY_BF16,
        "q8_0": gguf.LlamaFileType.MOSTLY_Q8_0,
        "tq1_0": gguf.LlamaFileType.MOSTLY_TQ1_0,
        "tq2_0": gguf.LlamaFileType.MOSTLY_TQ2_0,
        "auto": gguf.LlamaFileType.GUESSED,
    }

    is_split = args.split_max_tensors > 0 or args.split_max_size != "0"
    if args.use_temp_file and is_split:
        logger.error("Error: Cannot use temp file when splitting")
        sys.exit(1)

    if args.outfile is not None:
        fname_out = args.outfile
    elif hf_repo_id:
        # if remote, use the model ID as the output file name
        fname_out = Path("./" + hf_repo_id.replace("/", "-") + "-{ftype}.gguf")
    else:
        fname_out = dir_model

    logger.info(f"Loading model: {dir_model.name}")

    if args.mmproj:
        if "mmproj" not in fname_out.name:
            fname_out = ModelBase.add_prefix_to_filename(fname_out, "mmproj-")

    with torch.inference_mode():
        output_type = ftype_map[args.outtype]
        model_type = ModelType.MMPROJ if args.mmproj else ModelType.TEXT
        hparams = ModelBase.load_hparams(dir_model)
        model_architecture = get_model_architecture(hparams, model_type)
        logger.info(f"Model architecture: {model_architecture}")
        try:
            model_class = ModelBase.from_model_architecture(model_architecture, model_type=model_type)
        except NotImplementedError:
            logger.error(f"Model {model_architecture} is not supported")
            sys.exit(1)

        model_instance = model_class(dir_model, output_type, fname_out,
                                     is_big_endian=args.bigendian, use_temp_file=args.use_temp_file,
                                     eager=args.no_lazy,
                                     metadata_override=args.metadata, model_name=args.model_name,
                                     split_max_tensors=args.split_max_tensors,
                                     split_max_size=split_str_to_n_bytes(args.split_max_size), dry_run=args.dry_run,
                                     small_first_shard=args.no_tensor_first_split,
                                     remote_hf_model_id=hf_repo_id)

        if args.vocab_only:
            logger.info("Exporting model vocab...")
            model_instance.write_vocab()
            logger.info(f"Model vocab successfully exported to {model_instance.fname_out}")
        else:
            logger.info("Exporting model...")
            model_instance.write()
            out_path = f"{model_instance.fname_out.parent}{os.sep}" if is_split else model_instance.fname_out
            logger.info(f"Model successfully exported to {out_path}")


if __name__ == '__main__':
    main()
