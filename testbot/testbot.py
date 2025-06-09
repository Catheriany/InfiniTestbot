import os
import subprocess
import time
import pycurl
import json
from io import BytesIO
import platform
import abc
import json
import schedule
from datetime import datetime


CONFIG_JSON = "config.json"


class CmdResult:
    def __init__(self, result: subprocess.CompletedProcess, name: str = ""):
        self.stderr = result.stderr
        self.stdout = result.stdout
        self.returncode = result.returncode
        self.args = result.args
        self.name = f"任务: {name}" if name != "" else f"指令: {result.args}"


class Notifier(abc.ABC):
    def notify_results(self, meta):
        raise NotImplementedError()


class FeishuNotifier(Notifier):
    def __init__(self, config):
        self.webhook_url = config["url"]

    def notify_results(self, meta):
        results = meta["results"]
        messages = []
        is_success = "自动测试成功"
        for result in results:
            if result.returncode != 0:
                message = [f"{result.name} 失败 代号{result.returncode}\n"]
                message.append(f"输出: {result.stdout}\n")
                messages.append([{"tag": "text", "text": m} for m in message])
                is_success = "自动测试失败"
            else:
                message = f"{result.name}  成功.\n"
                messages.append([{"tag": "text", "text": message}])

        title = (
            "【"
            + is_success
            + "】"
            + meta["project"]
            + " 环境："
            + meta["env_name"]
            + " 分支："
            + meta["current_branch"]
        )

        # Webhook URL (Replace with your actual URL)
        url = self.webhook_url

        # JSON payload
        data = {
            "msg_type": "post",
            "content": {"post": {"zh_cn": {"title": title, "content": messages}}},
        }

        # Convert data to JSON string
        json_data = json.dumps(data)
        print("JSON data:", json_data)

        # Buffer to store response
        buffer = BytesIO()

        # Initialize cURL request
        c = pycurl.Curl()
        c.setopt(c.URL, url)
        c.setopt(c.POST, 1)
        c.setopt(c.HTTPHEADER, ["Content-Type: application/json"])
        c.setopt(c.POSTFIELDS, json_data)  # Send JSON data
        c.setopt(c.WRITEDATA, buffer)  # Store response in buffer
        c.perform()
        c.close()

        # Get response
        response = buffer.getvalue().decode("utf-8")
        print("Response:", response)


def build_notifier(config) -> Notifier:
    if config is None:
        return None
    if config.get("type", "") == "Feishu":
        return FeishuNotifier(config)
    return None


class TestBot:
    def __init__(self, config):
        self.project = config["project"]
        self.env_name = config["env_name"]
        self.repo_url = config["repo_url"]
        self.branches = config["branches"]
        self.current_branch = "default"
        self.results = []
        self.notifier = build_notifier(config.get("notifier", None))

        repo_name = self.repo_url.split("/")[-1]
        assert repo_name.endswith(".git"), "Repo URL must end with.git"
        self.working_dir = os.getcwd()
        self.project_dir = os.path.join(self.working_dir, repo_name.split(".")[0])

    def test_cmd(self, cmd, trials=1, break_on_error=True, name=""):
        trial = 0
        while trial < trials:
            trial += 1
            print(f"Running command: {cmd}")
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, encoding="utf-8"
            )
            if result.returncode != 0:
                print(
                    f"Trial {trial}/{trials} failed with error code {result.returncode}"
                )
                print(f"Output: {result.stderr}")
                if trial >= trials:
                    print("Maximum number of trials reached, failed.")
                    self.results.append(CmdResult(result, name))
                    if break_on_error:
                        raise RuntimeError(
                            f"Command {cmd} failed with error code {result.returncode}"
                        )
                    return
                else:
                    time.sleep(1)
            else:
                self.results.append(CmdResult(result, name))
                return

    def checkout_branch(self, branch):
        try:
            os.chdir(self.project_dir)
            self.test_cmd(f"git fetch origin {branch}", trials=5, name="Fetch")
            self.test_cmd(f"git reset --hard origin/{branch}", trials=5)
            self.current_branch = branch
        except:
            raise RuntimeError(f"Failed to checkout branch {branch}.")

    def clone_or_update(self):
        try:
            if not os.path.exists(self.project_dir):
                print(f"Cloning {self.repo_url}...")
                self.test_cmd(f"git clone {self.repo_url}", trials=5, name="Clone")
        except:
            raise RuntimeError(f"Failed to clone or update repository.")

    def notify_results(self):
        if self.notifier is not None:
            self.notifier.notify_results(self.__dict__)
        self.results = []

    def run_tests(self):
        raise NotImplementedError()


class InfiniCoreTestBot(TestBot):
    DEVICE_FLAGS = {
        "cpu": ("", ""),
        "nvidia": ("--nv-gpu=true", "--nvidia"),
        "cambricon": ("--cambricon-mlu=true", "--cambricon"),
        "ascend": ("--ascend-npu=true", "--ascend"),
    }

    def get_xmake_config_flags(self):
        return InfiniCoreTestBot.DEVICE_FLAGS[self.device_type][0]

    def get_python_test_flags(self):
        return InfiniCoreTestBot.DEVICE_FLAGS[self.device_type][1]

    def __init__(self, config):
        super().__init__(config)
        self.device_type = config.get("device_type", "cpu")

        if os.environ.get("INFINI_ROOT") == None:
            os.environ["INFINI_ROOT"] = os.path.expanduser("~/.infini")
        # Detect OS
        if platform.system() == "Windows":
            # Update PATH for Windows
            new_path = os.path.expanduser(os.environ.get("INFINI_ROOT") + "/bin")
            if new_path not in os.environ.get("PATH"):
                os.environ["PATH"] = f"{new_path};{os.environ.get('PATH', '')}"
        elif platform.system() == "Linux":
            new_path = os.path.expanduser(os.environ.get("INFINI_ROOT") + "/bin")
            if new_path not in os.environ.get("PATH"):
                os.environ["PATH"] = f"{new_path}:{os.environ.get('PATH', '')}"
            new_lib_path = os.path.expanduser(os.environ.get("INFINI_ROOT") + "/lib")
            if new_lib_path not in os.environ.get("LD_LIBRARY_PATH"):
                os.environ["LD_LIBRARY_PATH"] = (
                    f"{new_lib_path}:{os.environ.get('LD_LIBRARY_PATH', '')}"
                )
        else:
            raise RuntimeError("Unsupported platform.")

        self.infini_root = os.environ.get("INFINI_ROOT")

    def install(self, config_flags=""):
        name = "安装InfiniCore"
        try:
            os.chdir(self.project_dir)
            self.test_cmd(f"python scripts/install.py {config_flags}", name=name)

        except:
            raise

    def run_python_tests(self, flags=""):
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_file = os.path.join(self.project_dir, f"python_test_{timestamp}.log")

        try:
            cmd = f"python scripts/python_test.py {flags}".strip()
            with open(log_file, "w", encoding="utf-8", errors="replace") as log:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=self.project_dir,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if result.returncode != 0:
                    log.write(f"\n[ERROR] Python tests failed with return code {result.returncode}\n")
                    raise subprocess.CalledProcessError(result.returncode, cmd)
        except Exception as e:
            raise


    def run_gguf_tests(self, flags=""):
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_file = os.path.join(self.project_dir, f"gguf_test_{timestamp}.log")

        try:
            cmd = f"python scripts/gguf_test.py {flags}".strip()
            with open(log_file, "w", encoding="utf-8", errors="replace") as log:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=self.project_dir,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace"
                )
                if result.returncode != 0:
                    log.write(f"\n[ERROR] GGUF test failed with return code {result.returncode}\n")
                    raise subprocess.CalledProcessError(result.returncode, cmd)

        except Exception as e:
            raise


    def run_infiniccl_test(self, flags=""):
        os.chdir(self.project_dir)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_file = os.path.join(self.project_dir, f"infiniccl_test_{timestamp}.log")
        
        with open(log_file, "w", encoding="utf-8", errors="replace") as log:

            def run_and_log(cmd, header):
                log.write(f"\n=== {header} ===\n")
                result = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=self.project_dir,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if result.returncode != 0:
                    log.write(f"\n[ERROR] '{cmd}' exited with code {result.returncode}\n")
                    raise subprocess.CalledProcessError(result.returncode, cmd)

            run_and_log("xmake build infiniccl-test", "Building infiniccl-test")
            run_and_log("xmake install", "Installing infiniccl-test")
            exe_cmd = f"build/linux/x86_64/release/infiniccl-test {flags}"
            run_and_log(exe_cmd, "Running infiniccl-test")


    def run_infinirt_test(self, flags=""):
        os.chdir(self.project_dir)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_file = os.path.join(self.project_dir, f"infinirt_test_{timestamp}.log")

        with open(log_file, "w", encoding="utf-8", errors="replace") as log:

            def run_and_log(cmd, header):
                log.write(f"\n=== {header} ===\n")
                result = subprocess.run(
                    cmd,
                    shell=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=self.project_dir,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if result.returncode != 0:
                    log.write(f"\n[ERROR] Command failed with exit code {result.returncode}\n")
                    raise subprocess.CalledProcessError(result.returncode, cmd)

            run_and_log("xmake build infinirt-test", "Building infinirt-test")
            run_and_log("xmake install", "Installing infinirt-test")

            exe_cmd = f"build/linux/x86_64/release/infinirt-test {flags}"
            run_and_log(exe_cmd, "Running infinirt-test")


    def run_tests(self):
        def _run_test():
            self.install(self.get_xmake_config_flags())
            self.run_python_tests(self.get_python_test_flags())
            self.run_gguf_tests(self.get_python_test_flags())
            self.run_infiniccl_test(self.get_python_test_flags())
            self.run_infinirt_test(self.get_python_test_flags())

        try:
            self.clone_or_update()
            if self.branches is None or len(self.branches) == 0:
                _run_test()
            else:
                for branch in self.branches:
                    self.checkout_branch(branch)
                    _run_test()
        except:
            self.notify_results()
            raise

        self.notify_results()


def build_testbots_from_json(json_file_path):
    bots = []
    with open(json_file_path, "r", encoding="utf-8") as f:
        config = json.load(f)
        for test in config["tests"]:
            if test["project"] == "InfiniCore":
                bots.append(InfiniCoreTestBot(test))
    return bots


def main():
    try:
        bots = build_testbots_from_json(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_JSON)
        )

        for bot in bots:
            bot.run_tests()
    except:
        time.sleep(60)


schedule.every().day.at("00:00").do(lambda: main())

if __name__ == "__main__":
    while True:
        schedule.run_pending()
        time.sleep(1)
