import argparse
import torch.autograd.profiler as profiler
from utils.dist_args_utils import *
from utils.dist_inference_utils import *
from comm.comm_utils import *
from task_datasets.inference_data import get_request_processor
from pipeline_parallel.dist_pp_utils import *
from transformers import AutoTokenizer
from coordinator.crusoe.crusoe_coordinator_vm_client import VMClient


def main():
    parser = argparse.ArgumentParser(description='Inference Runner')
    add_device_arguments(parser)
    add_torch_distributed_arguments(parser)
    add_inference_arguments(parser)
    add_inference_details_arguments(parser)
    add_torch_distributed_inference_w_crusoe_coordinator_arguments(parser)
    parser.add_argument('--seed', type=int, default=1, metavar='S',
                        help='random seed (default: 1)')
    parser.add_argument('--overwrite-request-args', type=lambda x: (str(x).lower() == 'true'),
                        default=False, metavar='S',
                        help='whether overwrite_request_args')
    parser.add_argument('--profiling', type=str, default='tidy_profiling', metavar='S',
                        help='enable which profiling? default: tidy mode')
    parser.add_argument('--trace-postfix', type=str, default='default', metavar='S',
                        help='postfix of the tracing file name.')
    args = parser.parse_args()
    torch.manual_seed(args.seed)
    if args.use_cuda:
        assert (torch.cuda.is_available())
        device = torch.device('cuda', args.cuda_id)
    else:
        device = torch.device('cpu')

    init_communicators(args)

    if get_pipeline_parallel_rank() == 0:
        client = VMClient(args)
        client.send_message_to_coordinate("Inference job starts.")
    else:
        client = None

    if get_pipeline_parallel_rank() == 0 or True:
        request_processor = get_request_processor(args)
        request_processor.set_arguments(args)
    else:
        tokenizer = None
        request_processor = None
        print('warning: todo: arguments specified in the request will not take effect.')

    pipe = get_pp_inference_module(args, device)

    if args.profiling == 'no-profiling':
        distributed_inference_mask_iter(args, pipe, device, request_processor, vm_client=client)
    else:
        prefix = './trace_json/inference_' + args.pp_mode
        trace_file = prefix + get_inference_arguments_str(args) + '_' + args.profiling + '_' + args.trace_postfix + \
                     '.json'
        if args.profiling == 'tidy_profiling':
            # distributed_inference_mask_iter(args, pipe, device, request_processor)
            distributed_inference_mask_iter(args, pipe, device, request_processor, vm_client=client)
            pipe.export_profiling_result(filename=trace_file)
        elif args.profiling == 'pytorch_profiling':
            with profiler.profile(profile_memory=True, use_cuda=args.use_cuda) as prof:
                distributed_inference_mask_iter(args, pipe, device, request_processor, vm_client=client)
            print(prof.key_averages().table())
            prof.export_chrome_trace(trace_file)
        else:
            print("No recognized profiler?")
            assert False


if __name__ == '__main__':
    main()
