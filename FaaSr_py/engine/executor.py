import json
import logging
import os
import subprocess
import sys
from multiprocessing import Process
from pathlib import Path

import requests

from FaaSr_py.config.debug_config import global_config
from FaaSr_py.engine.faasr_payload import FaaSrPayload
from FaaSr_py.helpers.faasr_start_invoke_helper import \
    faasr_func_dependancy_install
from FaaSr_py.helpers.s3_helper_functions import (flush_s3_log,
                                                  get_invocation_folder)
from FaaSr_py.s3_api import faasr_put_file
from FaaSr_py.server.faasr_server import run_server, wait_for_server_start

logger = logging.getLogger(__name__)


class Executor:
    """
    Handles logic related to running user function
    """

    def __init__(self, faasr: FaaSrPayload):
        if not isinstance(faasr, FaaSrPayload):
            err_msg = "initializer for Executor must be FaaSr instance"
            logger.error(err_msg)
            sys.exit(1)
        self.faasr = faasr
        self.server = None
        self.packages = []

    def call(self, action_name):
        """
        Runs a user function given action name

        Arguments:
            action_name: str -- name of the action to run
        """
        func_name = self.faasr["ActionList"][action_name]["FunctionName"]
        func_type = self.faasr["ActionList"][action_name]["Type"]
        if "PackageImports" in self.faasr:
            imports = self.faasr["PackageImports"].get(func_name)
        else:
            imports = []
        user_args = self.get_user_function_args()

        if not global_config.SKIP_USER_FUNCTION:
            try:
                if func_type == "Python":
                    # entry script for py function
                    from FaaSr_py.client.py_user_func_entry import \
                        run_py_function

                    # run user func as seperate process
                    py_func = Process(
                        target=run_py_function,
                        args=(self.faasr, func_name, user_args, imports),
                    )

                    logger.info(f"Starting function: {func_name} (Python)")
                    py_func.start()
                    py_func.join()

                    if py_func.exitcode != 0:
                        raise RuntimeError(
                            f"non-zero exit code ({py_func.exitcode}) from Python function"
                        )
                elif func_type == "R":
                    # path to R function handler
                    r_entry_dir = Path(__file__).parent.parent / "client"
                    r_entry_path = r_entry_dir / "r_user_func_entry.R"

                    logger.info(f"Starting function: {func_name} (R)")

                    # run R entry as a subprocess
                    r_func = subprocess.run(
                        [
                            "Rscript",
                            str(r_entry_path),
                            func_name,
                            json.dumps(user_args),
                            self.faasr["InvocationID"],
                        ],
                        cwd=r_entry_dir,
                    )
                    if r_func.returncode != 0:
                        raise RuntimeError(
                            f"non-zero exit code ({r_func.returncode}) from R function"
                        )
            except Exception as e:
                raise RuntimeError("Error running user function") from e
        else:
            logger.info("SKIPPING USER FUNCTION")

        # At this point, the action has finished the invocation of the user Function
        # We flag this by uploading a file with the name FunctionInvoke.done to the S3 logs folder
        # Check if directory already exists. If not, create one
        log_folder = get_invocation_folder(self.faasr)
        log_folder_path = f"/tmp/{log_folder}/{self.faasr['FunctionInvoke']}/flag/"
        if not os.path.isdir(log_folder_path):
            os.makedirs(log_folder_path)
        if "FunctionRank" in self.faasr:
            file_name = (
                f"{self.faasr['FunctionInvoke']}.{self.faasr["FunctionRank"]}.done"
            )
        else:
            file_name = f"{self.faasr['FunctionInvoke']}.done"
        with open(f"{log_folder_path}/{file_name}", "w") as f:
            f.write("True")

        # Put .done file in S3
        faasr_put_file(
            faasr_payload=self.faasr,
            local_folder=log_folder_path,
            local_file=file_name,
            remote_folder=log_folder,
            remote_file=file_name,
        )

    def run_func(self, action_name, start_time):
        """
        Fetch and run the users function

        Arguments:
            action_name: str -- name of the action to run
        """
        # install dependencies for function
        logger.debug("Starting dependency install")
        action = self.faasr["ActionList"][action_name]
        faasr_func_dependancy_install(self.faasr, action)
        logger.debug("Finished installing dependencies")

        # Run function
        try:
            self.host_server_api(start_time=start_time)
            self.call(action_name)
            function_result = self.get_function_return()
        except SystemExit as e:
            raise e
        except RuntimeError as e:
            raise e
        except Exception as e:
            raise e
        finally:
            # Clean up server
            self.terminate_server()
        return function_result

    def host_server_api(self, start_time, port=8000):
        """
        Starts RPC server for serverside API

        Arguments:
            port: int -- port to run the server on
        """
        logger.info(f"Starting server on localhost port {port}")
        # flush s3 log since server process will be logging
        flush_s3_log()
        self.server = Process(target=run_server, args=(self.faasr, port, start_time))
        self.server.start()
        logger.debug("Polling localhost")
        wait_for_server_start(port)

    def terminate_server(self):
        """
        Terminate RPC server
        """
        if isinstance(self.server, Process):
            self.server.terminate()
        else:
            err_msg = "Tried to terminate server, but no server running"
            logger.error(err_msg)
            sys.exit(1)

    def get_user_function_args(self):
        """
        Returns user function arguments

        Returns:
            dict -- user function arguments
        """
        user_action = self.faasr["FunctionInvoke"]

        args = self.faasr["ActionList"][user_action]["Arguments"]
        if args is None:
            return {}
        else:
            return args

    def get_function_return(self, port=8000):
        """
        Get user function result

        Arguments:
            port: int -- port to get the function result from

        Returns:
            result: bool | None
        """
        try:
            return_response = requests.get(f"http://127.0.0.1:{port}/faasr-get-return")
            return_val = return_response.json()
        except requests.exceptions.RequestException as e:
            err_msg = "REQUESTS ERROR GETTING FUNCTION RESULT"
            logger.exception(err_msg, stack_info=True)
            raise RuntimeError(err_msg) from e
        except Exception as e:
            err_msg = f"UNKOWN ERROR GETTING FUNCTION RESULT -- {e}"
            logger.exception(err_msg, stack_info=True)
            raise RuntimeError(err_msg) from e

        if return_val.get("Error"):
            if return_val.get("Message"):
                err_msg = f"{return_val["Message"]}"
            else:
                err_msg = "Unkown error while getting user function args"
            logger.error(err_msg, stack_info=True)
            raise RuntimeError(err_msg)

        return return_val["FunctionResult"]
