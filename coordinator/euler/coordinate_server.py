import random
import socket
import argparse
from collections import OrderedDict
import os
import requests
import json


def server_message_parser(msg: bytes):
    msg_arg = msg.decode().split('#')
    arg_dict = {'task': msg_arg[0],
                'state': msg_arg[1]}
    if arg_dict['task'] == 'train':
        if arg_dict['state'] == 'finish':
            arg_dict['rank'] = int(msg_arg[2])
            arg_dict['iter_time'] = float(msg_arg[3])
        elif arg_dict['state'] == 'submit':
            arg_dict['job_name'] = msg_arg[2]
    if arg_dict['task'] == 'inference':
        if arg_dict['state'] == 'finish':
            arg_dict['rank'] = int(msg_arg[2])
            arg_dict['iter_time'] = float(msg_arg[3])
        elif arg_dict['state'] == 'submit':
            arg_dict['job_name'] = msg_arg[2]
            if len(msg_arg) > 3:
                arg_dict['infer_data'] = msg_arg[3]
        elif arg_dict['state'] == 'join':
            if len(msg_arg) > 2:
                arg_dict['node_type'] = msg_arg[2]
        elif arg_dict['state'] == 'latency_job':
            arg_dict['job_details'] = msg_arg[2]
    return arg_dict


def inference_latency_job_parser(job_detail_msg: str):
    return json.loads(job_detail_msg)


class CoordinatorTrainServer:
    def __init__(self, args):
        self.host = args.coordinator_server_ip
        self.port = args.coordinator_server_port
        self.rank_to_be_allocated = set()
        # An array of dict object to store worker info
        self.worker_nodes = OrderedDict()
        self.prime_worker_ip = None
        self.bsub_script_path = args.bsub_script_path
        self.train_demand_workers = 0

    '''
    def _get_rank0_ip(self):
        assert len(self.worker_nodes) > 0 and self.prime_worker_ip is not None
        return self.prime_worker_ip
    '''

    def _print_current_working_nodes(self):
        print("<----------------Current Working Nodes---------------->")
        for node_key in self.worker_nodes.keys():
            print(f"Node rank {self.worker_nodes[node_key]['rank']}, Address: {node_key}")
        print("-------------------------------------------------------")
        print("<-----------------Rank to be allocated----------------->")
        print(self.rank_to_be_allocated)

    def _handle_train_submit(self, job_name) -> str:
        print("<<<<<<<<<<<<<<<<<<<<< Submit Job >>>>>>>>>>>>>>>>>>>>>>")
        if self.train_demand_workers != 0:
            return 'Current training task is still running, cannot handle your job submission.'
        else:
            if job_name == 'lsf_gpt3xl_3gpu':
                self.train_demand_workers = 3
            elif job_name == 'lsf_gpt3xl_8gpu':
                self.train_demand_workers = 8
            elif job_name == 'lsf_gpt3xl_64gpu':
                self.train_demand_workers = 64
            else:
                return f'This job is not recognized on coordinate - {job_name}'

            self.rank_to_be_allocated.update(list(range(self.train_demand_workers)))

            for i in range(self.train_demand_workers):
                os.system(f"rm {self.bsub_script_path}/submit_cache/*.bsub")
                os.system(f"cp {self.bsub_script_path}/{job_name}.bsub "
                          f"{self.bsub_script_path}/submit_cache/{job_name}_{i+1}.bsub")
                os.system(f"echo \'--lsf-job-no {i+1}\' >> {self.bsub_script_path}/submit_cache/{job_name}_{i+1}.bsub")
                os.system(f"cd {self.bsub_script_path}/submit_cache && "
                          f"bsub < {job_name}_{i+1}.bsub")
            os.system("bjobs")
            return f'Succeed to submit job - {job_name}'

    def _handle_train_join(self, worker_ip, port) -> str:
        node_key = worker_ip + ':' + str(port)
        assert node_key not in self.worker_nodes, f"Worker called notify_train_join has been joined before ({node_key})"
        print(f"Connected by +NEW+ worker with address {worker_ip}, (port:{port})")
        new_node_rank = len(self.worker_nodes)
        if new_node_rank == 0:
            self.prime_worker_ip = worker_ip
        self.rank_to_be_allocated.remove(new_node_rank)
        self.worker_nodes[node_key] = {'rank': new_node_rank}
        return_msg = self.prime_worker_ip + '#' + str(self.worker_nodes[node_key]['rank'])
        return return_msg

    def _handle_train_finish(self, worker_ip, port, msg_arg) -> str:
        node_key = worker_ip + ':' + str(port)
        assert node_key in self.worker_nodes, f"Worker called notify_train_finish is not recognized ({node_key})"
        print(f"Connected by known worker with address {worker_ip}, (port:{port}), allocated rank "
              f"{self.worker_nodes[node_key]['rank']}")
        print(f"<=====Training finished on rank-{msg_arg['rank']} worker, "
              f"average time {msg_arg['iter_time']} seconds.=====>")
        del self.worker_nodes[node_key]
        self.train_demand_workers -= 1
        return_msg = 'done'
        return return_msg

    def _handle_train_message(self, worker_ip, port, msg_arg):
        pass

    def execute_server(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((self.host, self.port))
            s.listen()
            while True:
                connection, address = s.accept()
                with connection:
                    worker_ip, port = address
                    msg_data = connection.recv(1024)
                    print(f"==[Recv message: {msg_data}]==")
                    msg_arg = server_message_parser(msg_data)
                    if msg_arg['task'] == 'train':
                        if msg_arg['state'] == 'submit':
                            return_msg = self._handle_train_submit(msg_arg['job_name'])
                        elif msg_arg['state'] == 'join':
                            return_msg = self._handle_train_join(worker_ip, port)
                        elif msg_arg['state'] == 'finish':
                            return_msg = self._handle_train_finish(worker_ip, port, msg_arg)
                        else:
                            assert False, f"Not valid operator for training ({msg_arg['state']})"
                    connection.sendall(return_msg.encode())
                    connection.close()
                    self._print_current_working_nodes()


class CoordinatorInferenceServer:
    def __init__(self, args):
        self.host = args.coordinator_server_ip
        self.port = args.coordinator_server_port
        self.allocated_index = 0
        self.current_nccl_port = 15000
        # An array of dict object to store worker info
        self.working_pipelines = []
        self.prime_worker_ips = []
        self.active_inference_pipeline = 0
        self.bsub_script_path = args.bsub_script_path
        self.is_hybrid_task = False
        self.inference_pipeline_demand_worker_num = 0
        self.inference_pipeline_demand_GPU_worker_num = 0
        self.inference_pipeline_demand_CPU_worker_num = 0
        self.submit_locked = False

    def _allocate_index(self):
        self.allocated_index = (self.allocated_index + 1) % 10000
        return self.allocated_index

    def _print_current_working_nodes(self):
        print(f"<----------------Current Working Pipelines [{len(self.working_pipelines)}]---------------->")
        for i in range(len(self.working_pipelines)):
            print(f"<----------------Current Pipeline [{i}] Workers---------------->")
            for node_key in self.working_pipelines[i].keys():
                print(f"Node rank {self.working_pipelines[i][node_key]['rank']}, Address: {node_key}")
        print("-------------------------------------------------------")

    def _handle_inference_submit(self, job_name, infer_data) -> str:
        print("<<<<<<<<<<<<<<<<<<<<< Submit Job >>>>>>>>>>>>>>>>>>>>>>")
        if not self.submit_locked:
            self.submit_locked = True
            if job_name == 'lsf_gptJ_inf_4RTX2080Ti' or job_name == 'lsf_gptJ_inf_4RTXTitan.bsub':
                self.inference_pipeline_demand_worker_num = 4
            elif job_name == 'lsf_gptj':
                self.inference_pipeline_demand_worker_num = 2
            elif job_name == 'lsf_gptneox':
                self.inference_pipeline_demand_worker_num = 11
            elif job_name == 'lsf_opt66':
                self.inference_pipeline_demand_worker_num = 32
            elif job_name == 'lsf_opt66_16':
                self.inference_pipeline_demand_worker_num = 16
            elif job_name == 'lsf_t5':
                self.inference_pipeline_demand_worker_num = 6
            elif job_name == 'lsf_t0pp':
                self.inference_pipeline_demand_worker_num = 6
            elif job_name == 'lsf_ul2':
                self.inference_pipeline_demand_worker_num = 16
            elif job_name == 'lsf_hybrid_opt175b':
                self.is_hybrid_task = True
                self.inference_pipeline_demand_worker_num = 62
                self.inference_pipeline_demand_GPU_worker_num = 32
                self.inference_pipeline_demand_CPU_worker_num = 30
            else:
                return f'This job is not recognized on coordinate - {job_name}'

            if self.is_hybrid_task:
                for i in range(self.inference_pipeline_demand_worker_num):
                    os.system(f"rm {self.bsub_script_path}/submit_cache/*.bsub")
                    hardware = "gpu" if i < self.inference_pipeline_demand_GPU_worker_num else "cpu"
                    os.system(f"cp {self.bsub_script_path}/{job_name}_{hardware}.bsub "
                              f"{self.bsub_script_path}/submit_cache/{job_name}_{hardware}_{i+1}.bsub")
                    os.system(f"echo \'--lsf-job-no {self._allocate_index()} --infer-data {infer_data}\' "
                              f">> {self.bsub_script_path}/submit_cache/{job_name}_{hardware}_{i+1}.bsub")
                    os.system(f"cd {self.bsub_script_path}/submit_cache && "
                              f"bsub < {job_name}_{hardware}_{i+1}.bsub")
                os.system("bjobs")
                self.working_pipelines.append(OrderedDict())
                self.active_inference_pipeline += 1
                return f'Succeed to submit job - {job_name}'
            else:
                for i in range(self.inference_pipeline_demand_worker_num):
                    os.system(f"rm {self.bsub_script_path}/submit_cache/*.bsub")
                    os.system(f"cp {self.bsub_script_path}/{job_name}.bsub "
                              f"{self.bsub_script_path}/submit_cache/{job_name}_{i+1}.bsub")
                    os.system(f"echo \'--lsf-job-no {self._allocate_index()} --infer-data {infer_data}\' >> {self.bsub_script_path}/submit_cache/{job_name}_{i+1}.bsub")
                    os.system(f"cd {self.bsub_script_path}/submit_cache && "
                              f"bsub < {job_name}_{i+1}.bsub")
                os.system("bjobs")
                self.working_pipelines.append(OrderedDict())
                self.active_inference_pipeline += 1
                return f'Succeed to submit job - {job_name}'
        else:
            return f'Fail to submit job - {job_name}, coordinator server is handling other submission'

    def _check_if_node_has_joined(self, node_key):
        for pipe in self.working_pipelines:
            if node_key in pipe:
                return True
        return False

    def _get_node_working_pipeline_index(self, node_key):
        for i in range(len(self.working_pipelines)):
            if node_key in self.working_pipelines[i]:
                return i
        return -1

    def _handle_inference_join(self, worker_ip, port, node_type=None) -> str:
        node_key = worker_ip + ':' + str(port)
        assert not self._check_if_node_has_joined(node_key),\
            f"Worker called notify_inference_join has been joined before ({node_key})"
        print(f"Connected by +NEW+ worker with address {worker_ip}, (port:{port})")
        new_node_rank = len(self.working_pipelines[-1])
        if new_node_rank == 0:
            self.prime_worker_ips.append(worker_ip)
            self.current_nccl_port += 1 # make sure each inference job has different nccl_port.
        self.working_pipelines[-1][node_key] = {'rank': new_node_rank, 'nccl_port': self.current_nccl_port}
        if len(self.working_pipelines[-1]) == self.inference_pipeline_demand_worker_num:
            self.submit_locked = False
        # all nodes have the same random port
        # random.seed(self.allocated_index)
        # nccl_port = 15000 + random.randint(0, 1000)
        return_msg = self.prime_worker_ips[-1] + '#' + str(self.working_pipelines[-1][node_key]['rank'])
        return_msg += '#' + str(self.working_pipelines[-1][node_key]['nccl_port'])
        print(return_msg)
        return return_msg

    def _handle_inference_finish(self, worker_ip, port, msg_arg) -> str:
        node_key = worker_ip + ':' + str(port)
        pipe_index = self._get_node_working_pipeline_index(node_key)
        assert pipe_index != -1, f"Worker called notify_inference_finish is not recognized ({node_key})"
        print(f"Connected by known worker with address {worker_ip}, (port:{port}), Pipeline Index {pipe_index},"
              f" allocated rank {self.working_pipelines[pipe_index][node_key]['rank']}")
        print(f"<=====Inference finished on rank-{msg_arg['rank']} worker, "
              f"average time {msg_arg['iter_time']} seconds.=====>")
        if self.working_pipelines[pipe_index][node_key]['rank'] == 0:
            del self.prime_worker_ips[pipe_index]
        del self.working_pipelines[pipe_index][node_key]
        if len(self.working_pipelines[pipe_index]) == 0:
            del self.working_pipelines[pipe_index]
        return_msg = 'done'
        return return_msg


    def _handle_inference_job_latency(self, job_details) -> str:
        print("<<<<<<<<<<<<<<<<<<<<< Get Inference Job >>>>>>>>>>>>>>>>>>>>>>")
        # parser the inference job like
        '''
        url = 'http://10.205.11.33:5001'
        myobj = {
            'inputs': "you are not",
            "parameters": {
                "max_new_tokens": 20, "return_full_text": True,
                "do_sample": True, "temperature": 0.8, "top_p": 0.95,
                "max_time": 10.0, "num_return_sequences": 2,
                "use_gpu": True,
            }
        }
        '''
        inference_latency_job = inference_latency_job_parser(job_details)
        url = 'http://' + self.prime_worker_ips[0] + ':5001'
        x = requests.post(url, json=inference_latency_job)
        return x.text

    def execute_server(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((self.host, self.port))
            s.listen()
            while True:
                connection, address = s.accept()
                with connection:
                    worker_ip, port = address
                    msg_data = connection.recv(1024)
                    print(f"==[Recv message: {msg_data}]==")
                    msg_arg = server_message_parser(msg_data)
                    if msg_arg['task'] == 'inference':
                        if msg_arg['state'] == 'submit':
                            return_msg = self._handle_inference_submit(msg_arg['job_name'], msg_arg['infer_data'])
                        elif msg_arg['state'] == 'join':
                            return_msg = self._handle_inference_join(worker_ip, port, node_type=None)
                        elif msg_arg['state'] == 'finish':
                            return_msg = self._handle_inference_finish(worker_ip, port, msg_arg)
                        elif msg_arg['state'] == 'latency_job':
                            return_msg = self._handle_inference_job_latency(msg_arg['job_details'])
                        else:
                            assert False, f"Not valid operator for training ({msg_arg['state']})"
                    connection.sendall(return_msg.encode())
                    connection.close()
                    self._print_current_working_nodes()


class CoordinatorHybridInferenceServer:
    def __init__(self, args):
        self.host = args.coordinator_server_ip
        self.port = args.coordinator_server_port
        self.allocated_index = 0
        self.current_nccl_port = 15000
        # An array of dict object to store worker info
        self.prime_worker_ip = None
        self.gpu_nodes = {}
        self.cpu_nodes = {}
        self.bsub_script_path = args.bsub_script_path
        self.inference_pipeline_demand_GPU_worker_num = 0
        self.inference_pipeline_demand_CPU_worker_num = 0
        self.job_name = None
        self.infer_data = None
        self.submit_locked = False

    def _allocate_index(self):
        self.allocated_index = (self.allocated_index + 1) % 10000
        return self.allocated_index

    def _print_current_working_nodes(self):
        print(f"<----------------Current GPU Workers---------------->")
        for node_key in self.gpu_nodes.keys():
            print(f"Node rank {self.gpu_nodes[node_key]['rank']}, Address: {node_key}")
        print("-------------------------------------------------------")
        print(f"<----------------Current CPU Workers---------------->")
        for node_key in self.cpu_nodes.keys():
            print(f"Node rank {self.cpu_nodes[node_key]['rank']}, Address: {node_key}")
        print("-------------------------------------------------------")

    def _submit_gpu_tasks(self):
        hardware = "gpu"
        for i in range(self.inference_pipeline_demand_GPU_worker_num):
            os.system(f"rm {self.bsub_script_path}/submit_cache/*.bsub")
            os.system(f"cp {self.bsub_script_path}/{self.job_name}_{hardware}.bsub "
                      f"{self.bsub_script_path}/submit_cache/{self.job_name}_{hardware}_{i + 1}.bsub")
            os.system(f"echo \'--lsf-job-no {self._allocate_index()} --infer-data {self.infer_data}\' "
                      f">> {self.bsub_script_path}/submit_cache/{self.job_name}_{hardware}_{i + 1}.bsub")
            os.system(f"cd {self.bsub_script_path}/submit_cache && "
                      f"bsub < {self.job_name}_{hardware}_{i + 1}.bsub")
        os.system("bjobs")
        return f'Succeed to submit GPU job - {self.job_name}'

    def _submit_cpu_tasks(self):
        hardware = "cpu"
        total_num = self.inference_pipeline_demand_GPU_worker_num + self.inference_pipeline_demand_CPU_worker_num
        for i in range(self.inference_pipeline_demand_GPU_worker_num, total_num):
            os.system(f"rm {self.bsub_script_path}/submit_cache/*.bsub")
            os.system(f"cp {self.bsub_script_path}/{self.job_name}_{hardware}.bsub "
                      f"{self.bsub_script_path}/submit_cache/{self.job_name}_{hardware}_{i + 1}.bsub")
            os.system(f"echo \'--lsf-job-no {self._allocate_index()} --infer-data {self.infer_data}\' "
                      f">> {self.bsub_script_path}/submit_cache/{self.job_name}_{hardware}_{i + 1}.bsub")
            os.system(f"cd {self.bsub_script_path}/submit_cache && "
                      f"bsub < {self.job_name}_{hardware}_{i + 1}.bsub")
        os.system("bjobs")
        return f'Succeed to submit CPU job - {self.job_name}'

    def _handle_inference_submit(self, job_name, infer_data) -> str:
        print("<<<<<<<<<<<<<<<<<<<<< Submit Job >>>>>>>>>>>>>>>>>>>>>>")
        if not self.submit_locked:
            self.submit_locked = True
            if job_name == 'lsf_hybrid_opt175b':
                self.inference_pipeline_demand_GPU_worker_num = 4
                self.inference_pipeline_demand_CPU_worker_num = 4
                self.job_name = job_name
                self.infer_data = infer_data
            else:
                return f'This job is not recognized on coordinate - {job_name}'
            return self._submit_gpu_tasks()
        else:
            return f'Fail to submit job - {job_name}, coordinator server is handling other submission'

    def _check_if_node_has_joined(self, node_key):
        assert node_key not in self.cpu_nodes
        assert node_key not in self.gpu_nodes

    def _handle_inference_join(self, worker_ip, port, node_type: str) -> str:
        assert node_type in {'CPU','GPU'}
        node_key = worker_ip + ':' + str(port)
        assert not self._check_if_node_has_joined(node_key),\
            f"Worker called notify_inference_join has been joined before ({node_key})"
        print(f"Connected by +NEW+ {node_key} worker with address {worker_ip}, (port:{port}), ")

        if node_type == 'GPU':
            new_node_rank = len(self.gpu_nodes)
            if new_node_rank == 0:
                self.prime_worker_ip = worker_ip
            self.gpu_nodes[node_key] = {'rank': new_node_rank, 'port': self.current_nccl_port}
            # We only submit CPU tasks when all GPU nodes are ready.
            if len(self.gpu_nodes) == self.inference_pipeline_demand_GPU_worker_num:
                self._submit_cpu_tasks()
        else:
            assert node_type == 'CPU'
            new_node_rank = len(self.cpu_nodes) + self.inference_pipeline_demand_GPU_worker_num
            self.cpu_nodes[node_key] = {'rank': new_node_rank, 'port': self.current_nccl_port}
            if len(self.cpu_nodes) == self.inference_pipeline_demand_CPU_worker_num:
                print("All CPU and GPU nodes are ready.")
        # all nodes have the same random port
        # random.seed(self.allocated_index)
        # nccl_port = 15000 + random.randint(0, 1000)
        return_msg = self.prime_worker_ip + '#' + str(new_node_rank)
        return_msg += '#' + str(self.current_nccl_port)
        print(return_msg)
        return return_msg

    def _handle_inference_finish(self, worker_ip, port, msg_arg) -> str:
        node_key = worker_ip + ':' + str(port)
        assert node_key in self.cpu_nodes or node_key in self.gpu_nodes, \
            f"Worker called notify_inference_finish is not recognized ({node_key})"
        if node_key in self.gpu_nodes:
            assigned_rank = 'gpu_' + str(self.gpu_nodes[node_key]['rank'])
            del self.gpu_nodes[node_key]
        else:
            assigned_rank = 'cpu_' + str(self.cpu_nodes[node_key]['rank'])
            del self.cpu_nodes[node_key]

        print(f"Connected by known worker with address {worker_ip}, (port:{port})"
              f" allocated rank {assigned_rank}")
        print(f"<=====Inference finished on rank-{msg_arg['rank']} worker, "
              f"average time {msg_arg['iter_time']} seconds.=====>")
        if len(self.gpu_nodes) == 0 and len(self.cpu_nodes) == 0:
            self.submit_locked = False
        return_msg = 'done'
        return return_msg

    def execute_server(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((self.host, self.port))
            s.listen()
            while True:
                connection, address = s.accept()
                with connection:
                    worker_ip, port = address
                    msg_data = connection.recv(1024)
                    print(f"==[Recv message: {msg_data}]==")
                    msg_arg = server_message_parser(msg_data)
                    if msg_arg['task'] == 'inference':
                        if msg_arg['state'] == 'submit':
                            return_msg = self._handle_inference_submit(msg_arg['job_name'], msg_arg['infer_data'])
                        elif msg_arg['state'] == 'join':
                            return_msg = self._handle_inference_join(worker_ip, port, node_type=msg_arg['node_type'])
                        elif msg_arg['state'] == 'finish':
                            return_msg = self._handle_inference_finish(worker_ip, port, msg_arg)
                        else:
                            assert False, f"Not valid operator for training ({msg_arg['state']})"
                    connection.sendall(return_msg.encode())
                    connection.close()
                    self._print_current_working_nodes()


def main():
    parser = argparse.ArgumentParser(description='Test Coordinator-Server')
    parser.add_argument('--coordinator-type', type=str, default='train', help='train or inference')
    parser.add_argument('--coordinator-server-port', type=int, default=9002, metavar='N',
                        help='The port of coordinator-server.')
    parser.add_argument('--coordinator-server-ip', type=str, default='localhost', metavar='S',
                        help='The IP of coordinator-server.')
    parser.add_argument('--bsub-script-path', type=str,
                        default='/nfs/iiscratch-zhang.inf.ethz.ch/export/zhang/export/fm/GPT-home-private/coordinator/euler/lsf_scripts',
                        metavar='S', help='Path to store the bsub scripts')
    args = parser.parse_args()
    print(vars(args))
    if args.coordinator_type == 'train':
        coordinator = CoordinatorTrainServer(args)
    elif args.coordinator_type == 'inference':
        coordinator = CoordinatorInferenceServer(args)
    elif args.coordinator_type == 'hybrid_inference':
        coordinator = CoordinatorHybridInferenceServer(args)
    else:
        assert False
    coordinator.execute_server()


if __name__ == '__main__':
    main()
