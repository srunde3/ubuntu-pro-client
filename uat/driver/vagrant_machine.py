from dataclasses import dataclass
import os
import shlex
import subprocess
import tempfile
from typing import List, Optional, Union


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    return_code: int


class VagrantMachine:
    """Small host-side driver for the Vagrant VirtualBox system under test."""

    def __init__(self, vagrant_dir: str, name: str = "trusty-sut") -> None:
        self.vagrant_dir = os.path.abspath(vagrant_dir)
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def ip(self) -> str:
        result = self._run(["vagrant", "ssh-config"])
        if result.return_code:
            raise RuntimeError(result.stderr.strip())
        for line in result.stdout.splitlines():
            key, separator, value = line.strip().partition(" ")
            if key == "HostName" and separator:
                return value.strip()
        raise RuntimeError("Vagrant did not provide a HostName")

    def up(self) -> None:
        result = self._run(["vagrant", "up"])
        if result.return_code:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    def execute(
        self,
        command: Union[str, List[str]],
        stdin: Optional[str] = None,
        use_sudo: bool = False,
        **kwargs,
    ) -> CommandResult:
        if isinstance(command, list):
            command = " ".join(shlex.quote(item) for item in command)
        if use_sudo:
            command = "sudo -E bash -c {}".format(shlex.quote(command))
        result = self._run(["vagrant", "ssh", "-c", command], input_text=stdin)
        return result

    def push_file(self, local_path: str, remote_path: str) -> None:
        result = self._run(["vagrant", "upload", local_path, remote_path])
        if result.return_code:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    def install_deb(self, deb_path: str) -> CommandResult:
        remote_path = "/tmp/ubuntu-pro-uat.deb"
        self.push_file(deb_path, remote_path)
        result = self.execute(
            "dpkg -i {} || true; apt-get -f install --yes --no-install-recommends".format(
                shlex.quote(remote_path)
            ),
            use_sudo=True,
        )
        self.execute(["rm", "-f", remote_path], use_sudo=True)
        return result

    def pull_file(self, remote_path: str, local_path: str) -> None:
        with tempfile.TemporaryDirectory(prefix="vagrant-pull-") as temp_dir:
            temp_path = "/tmp/vagrant-pull-{}".format(os.getpid())
            result = self._run(
                [
                    "vagrant",
                    "ssh",
                    "-c",
                    "sudo cp {} {} && sudo chmod 0644 {}".format(
                        shlex.quote(remote_path),
                        shlex.quote(temp_path),
                        shlex.quote(temp_path),
                    ),
                ]
            )
            if result.return_code:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            config_path = os.path.join(temp_dir, "ssh-config")
            config_result = self._run(["vagrant", "ssh-config"])
            if config_result.return_code:
                raise RuntimeError(
                    config_result.stderr.strip() or config_result.stdout.strip()
                )
            with open(config_path, "w") as config_file:
                config_file.write(config_result.stdout)
            result = self._run(
                ["scp", "-F", config_path, "default:{}".format(temp_path), local_path]
            )
            if result.return_code:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            self.execute(["rm", "-f", temp_path])

    def reload(self) -> None:
        result = self._run(["vagrant", "reload"])
        if result.return_code:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    def destroy(self) -> None:
        result = self._run(["vagrant", "destroy", "--force"])
        if result.return_code:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    def _run(
        self, command: List[str], input_text: Optional[str] = None
    ) -> CommandResult:
        process = subprocess.run(
            command,
            cwd=self.vagrant_dir,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )
        return CommandResult(
            stdout=process.stdout,
            stderr=process.stderr,
            return_code=process.returncode,
        )
