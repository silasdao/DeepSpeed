#!/usr/bin/env python

# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team

from functools import partial
import argparse
import glob
import itertools
import multiprocessing
import os
import re
import shutil
import torch
import tqdm
# from pprint import pprint

from deepspeed.checkpoint import DeepSpeedCheckpoint
from deepspeed.checkpoint import (
    OPTIMIZER_STATE_DICT,
    BASE_OPTIMIZER_STATE,
    SINGLE_PARTITION_OF_FP32_GROUPS,
    PARAM_SLICE_MAPPINGS,
    PARAM_SHAPES,
    PARAM,
    CAT_DIM,
    VOCAB_DIVISIBILITY_PADDING_TENSOR,
    ORIGINAL_VOCAB_SIZE,
    UNIVERSAL_CHECKPOINT_INFO,
    VOCABULARY_PARAMETER_PATTERNS,
    PIPELINE_REPLICATED_PARAMETER_PATTERNS,
    TP_REPLICATED_PARAMETER_PATTERNS,
    PARAMETER_TO_AVERAGE_PATTERNS,
    PARAMETER_WITH_ROW_PARALLELISM_PATTERNS,
)


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input_folder', type=str, required=True, help='Input DeepSpeed Checkpoint folder')
    parser.add_argument('--output_folder', type=str, required=True, help='Output DeepSpeed checkpoint folder')
    parser.add_argument('--num_extract_workers',
                        default=4,
                        type=int,
                        help='How many parallel processes to extract zero shards')
    parser.add_argument(
        '--num_merge_workers',
        default=2,
        type=int,
        help=
        'How many parallel processes to merge tp slices (more memory intensive, use much fewer than --num_extract_workers))'
    )
    parser.add_argument('--keep_temp_folder',
                        action='store_true',
                        help='Preserve temporary folder of intermediate checkpoint slice files. Useful for debugging.')
    args = parser.parse_args()
    print(f'args = {args}')
    return args


def _create_checkpoint_paths(base_folder, iteration, tp_degree, pp_degree):
    path_list = []
    iter_folder = f'iter_{iteration:07d}'
    for i in range(0, tp_degree):
        path_list.append([])
        for j in range(0, pp_degree):
            rank_folder = f'mp_rank_{i:02d}' if pp_degree == 1 else f'mp_rank_{i:02d}_{j:03d}'
            ckpt_path = os.path.join(rank_folder, 'model_optim_rng.pt')
            path_list[i].append(os.path.join(base_folder, iter_folder, ckpt_path))

    return path_list


def _save_checkpoint(file_path, chkpt_sd):
    dir, _ = os.path.split(file_path)
    os.makedirs(dir, exist_ok=True)
    torch.save(chkpt_sd, file_path)


def extract_zero_shards(dir, ds_checkpoint, indices_3D):
    pp_index, tp_index, dp_index = indices_3D
    sd = ds_checkpoint.get_zero_checkpoint_state(pp_index=pp_index, tp_index=tp_index, dp_index=dp_index)

    # pprint(f"Processing {dp_index=} {pp_index=}, {tp_index=}")

    optim_sd = sd[OPTIMIZER_STATE_DICT]
    param_slice_mappings = optim_sd[PARAM_SLICE_MAPPINGS]
    universal_checkpoint_info = ds_checkpoint.get_checkpoint_info(UNIVERSAL_CHECKPOINT_INFO)
    pipeline_replicated_params = universal_checkpoint_info.get(PIPELINE_REPLICATED_PARAMETER_PATTERNS, [])
    # print(f'{pipeline_replicated_params=}')

    # dict
    state_groups = optim_sd[BASE_OPTIMIZER_STATE]["state"]
    # list
    fp32_groups = optim_sd[SINGLE_PARTITION_OF_FP32_GROUPS]
    param_groups_cnt = len(state_groups)

    for param_group_id in range(param_groups_cnt):

        flat_state = dict(
            exp_avg=state_groups[param_group_id]["exp_avg"],
            exp_avg_sq=state_groups[param_group_id]["exp_avg_sq"],
            fp32=fp32_groups[param_group_id],
        )

        for name, fragment_mapping in param_slice_mappings[param_group_id].items():
            if pp_index > 0 and any(re.match(pattern, name) for pattern in pipeline_replicated_params):
                # Skip tied weights that are replicated in first and last pp stages
                continue

            # pprint(f"dpt{dp_index}{pp_index}{tp_index} {param_group_id} {name} => {fragment_mapping.start}:{fragment_mapping.numel}")
            for state_key in flat_state:
                dump_param_fragment(dir, tp_index, dp_index, state_key, flat_state[state_key], name,
                                    fragment_mapping.start, fragment_mapping.numel)


cnt = 0


def dump_param_fragment(dir, tp_index, dp_index, state_name, state_flat_tensor, param_name, offset, numel):

    global cnt  # temp hack

    param_base_path = os.path.join(dir, param_name, str(tp_index))
    os.makedirs(param_base_path, exist_ok=True)

    cnt += 1
    counter = f"{dp_index:0>2d}"

    path = os.path.join(param_base_path, f"{state_name}.{counter}")

    #print(f"{param_name}: {offset}: {numel} => {path}")

    t = state_flat_tensor.narrow(0, offset, numel).clone()
    _save_checkpoint(path, t)


def _merge_zero_shards(param_base_path, state, tp_degree, slice_shape):
    slices = []
    for tp_index in range(tp_degree):
        prefix_path = os.path.join(param_base_path, str(tp_index), f"{state}")
        paths = sorted(list(glob.glob(f"{prefix_path}.*")))
        shards = [torch.load(p) for p in paths]
        slice = torch.cat(shards, dim=0).reshape(slice_shape)
        slices.append(slice)

    return slices


def _get_vocab_divisibility_padding_tensor(universal_checkpoint_info, padded_vocab_tensor):
    original_vocab_size = universal_checkpoint_info.get(ORIGINAL_VOCAB_SIZE)
    if padded_vocab_tensor.shape[0] > original_vocab_size:
        return padded_vocab_tensor[-1]
    else:
        return torch.zeros(padded_vocab_tensor.shape[1])


def merge_tp_slices(ds_checkpoint, dir, slice_dir, tp_degree, name_and_shape):
    name, shape = name_and_shape
    slice_base_path = os.path.join(slice_dir, name)
    param_base_path = os.path.join(dir, name)

    universal_checkpoint_info = ds_checkpoint.get_checkpoint_info(UNIVERSAL_CHECKPOINT_INFO)
    replicated_parameters = universal_checkpoint_info.get(TP_REPLICATED_PARAMETER_PATTERNS, [])
    parameters_to_average = universal_checkpoint_info.get(PARAMETER_TO_AVERAGE_PATTERNS, [])
    parameters_with_row_parallelism = universal_checkpoint_info.get(PARAMETER_WITH_ROW_PARALLELISM_PATTERNS, [])
    vocabulary_parameters = universal_checkpoint_info.get(VOCABULARY_PARAMETER_PATTERNS, [])
    for state in ("fp32", "exp_avg", "exp_avg_sq"):
        slices = _merge_zero_shards(slice_base_path, state, tp_degree, shape)
        final_path = os.path.join(param_base_path, f"{state}.pt")

        #print(f"Expected shape: {shape}")
        #print(f"Fragment sizes:", list(frag.shape for frag in slices))
        ckpt_dict = {}
        if any(re.match(pattern, name) for pattern in replicated_parameters):
            if len(slices) > 1:
                assert all(slices[0].equal(other_slice) for other_slice in slices[1:])
            param = slices[0]
                    # print(f'replicate {name} using first slice')
        elif any(re.match(pattern, name) for pattern in parameters_to_average):
            param = sum(slices) / len(slices)
            # print(f'merge {name} using average')
        else:
            cat_dim = 1 if any(re.match(pattern, name) for pattern in parameters_with_row_parallelism) else 0
            # print(f"merge {name} with CAT DIM: {cat_dim}")
            param = torch.cat(slices, dim=cat_dim)
            ckpt_dict[CAT_DIM] = cat_dim

        if any(re.match(pattern, name) for pattern in vocabulary_parameters):
            #print(f"Before {param.shape=}")
            # strip padding
            #param = _strip_vocab_padding(ds_checkpoint, param)
            ckpt_dict[VOCAB_DIVISIBILITY_PADDING_TENSOR] = _get_vocab_divisibility_padding_tensor(
                universal_checkpoint_info, param)
            #print(f"After {param.shape=}")

        #print(f"Final shape: {param.shape}")
        ckpt_dict[PARAM] = param
        _save_checkpoint(final_path, ckpt_dict)


def _get_chunks(l, n):
    for i in range(0, len(l), n):
        yield l[i:i + n]


def _do_parallel_work(do_work, work_chunks, num_workers):
    pool = multiprocessing.Pool(num_workers)
    for batch in tqdm.tqdm(work_chunks):
        pool.map(do_work, batch)
    pool.close()
    pool.join()


def _extract_zero_shard_files(args, ds_checkpoint, temp_dir):
    _3d_range_list = list(
        itertools.product(range(ds_checkpoint.pp_degree), range(ds_checkpoint.tp_degree),
                          range(ds_checkpoint.dp_degree)))
    # pprint(f'{_3d_range_list=}')
    work_chunks = list(_get_chunks(_3d_range_list, args.num_extract_workers))
    # pprint(f'{work_chunks=}')

    # extract_zero_shards(temp_dir, ds_checkpoint, _3d_range_list[0])
    do_work = partial(extract_zero_shards, temp_dir, ds_checkpoint)
    _do_parallel_work(do_work, work_chunks, args.num_extract_workers)


def _merge_tp_slice_files(args, ds_checkpoint, slice_shapes, temp_dir):
    work_chunks = list(_get_chunks(list(slice_shapes.items()), args.num_merge_workers))
    #pprint(work_chunks)
    zero_output_folder = os.path.join(args.output_folder, "zero")
    do_work = partial(merge_tp_slices, ds_checkpoint, zero_output_folder, temp_dir, ds_checkpoint.tp_degree)
    _do_parallel_work(do_work, work_chunks, args.num_merge_workers)


def _save_optimizer_state(args, ds_checkpoint):
    sharded_states = [BASE_OPTIMIZER_STATE, PARAM_SLICE_MAPPINGS, SINGLE_PARTITION_OF_FP32_GROUPS]
    sd = ds_checkpoint.get_zero_checkpoint_state(pp_index=0, tp_index=0, dp_index=0)

    optim_sd = sd[OPTIMIZER_STATE_DICT]
    output_sd = {k: v for k, v in optim_sd.items() if k not in sharded_states}
    zero_output_folder = os.path.join(args.output_folder, "zero")
    output_file_path = os.path.join(zero_output_folder, "optimizer_state.pt")
    _save_checkpoint(output_file_path, output_sd)


def _check_for_required_state(ds_checkpoint):
    universal_checkpoint_info = ds_checkpoint.get_checkpoint_info(UNIVERSAL_CHECKPOINT_INFO)
    assert universal_checkpoint_info is not None, f'Required {UNIVERSAL_CHECKPOINT_INFO} state is missing in checkpoint. Verify that client creates this state.'


def main():
    print('Convert DeepSpeed Checkpoint to Universal Checkpoint')

    args = parse_arguments()
    print(f'Converting DeepSpeed checkpoint in {args.input_folder} to Universal checkpoint in {args.output_folder}')

    ds_checkpoint = DeepSpeedCheckpoint(args.input_folder)
    _check_for_required_state(ds_checkpoint)

    iteration = ds_checkpoint.get_iteration()
    #_create_latest_file(args.output_folder, iteration)
    checkpoint_paths = _create_checkpoint_paths(args.output_folder, iteration, ds_checkpoint.tp_degree,
                                                ds_checkpoint.pp_degree)

    slice_shapes = []
    for mp_rank_file in ds_checkpoint.mp_rank_files:
        mp_sd = torch.load(mp_rank_file, map_location=torch.device('cpu'))
        slice_shapes += mp_sd[PARAM_SHAPES]

    # fix back to normal flat dict, merge duplicates for tp>1
    slice_shapes = {k: v for d in slice_shapes for k, v in d.items()}
    temp_dir = os.path.join(args.output_folder, 'tmp')

    print('*** 1. Extracting ZeRO fragments')
    _extract_zero_shard_files(args, ds_checkpoint, temp_dir)

    print('*** 2. Merging slices')
    _merge_tp_slice_files(args, ds_checkpoint, slice_shapes, temp_dir)

    print('*** 3. Saving common optimizer states')
    _save_optimizer_state(args, ds_checkpoint)

    if not args.keep_temp_folder:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Copy mp* files into output folder
    for f in glob.glob(os.path.join(args.input_folder, 'mp*')):
        shutil.copy2(f, args.output_folder)

    # Update latest to output folder
    checkpoint_root_folder, step_folder = os.path.split(args.output_folder)
    latest_file = os.path.join(checkpoint_root_folder, 'latest_universal')
    with open(latest_file, "w") as f:
        f.write(step_folder)

    print('*** Done!')


if __name__ == "__main__":
    main()
