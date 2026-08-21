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


class SshSession:
    """Direct ssh access to a Vagrant box, built from `vagrant ssh-config`.

    Vagrant's own `ssh -c` allocates a tty, which merges the remote command's
    stderr into stdout; going through ssh with `-T` keeps them apart.
    """

    HOST = "default"

    def __init__(self, config: str) -> None:
        handle = tempfile.NamedTemporaryFile(
            "w", prefix="vagrant-ssh-config-", delete=False
        )
        handle.write(config)
        handle.close()
        self._config_path = handle.name

    def command(self, command: str) -> List[str]:
        return ["ssh", "-F", self._config_path, "-T", self.HOST, command]

    def download(self, remote_path: str, local_path: str) -> List[str]:
        return [
            "scp",
            "-F",
            self._config_path,
            "{}:{}".format(self.HOST, remote_path),
            local_path,
        ]

    def close(self) -> None:
        if os.path.exists(self._config_path):
            os.unlink(self._config_path)


class VagrantMachine:
    """Small host-side driver for the Vagrant VirtualBox system under test."""

    def __init__(self, vagrant_dir: str, name: str = "trusty-sut") -> None:
        self.vagrant_dir = os.path.abspath(vagrant_dir)
        self._name = name
        self._session: Optional[SshSession] = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def ssh(self) -> SshSession:
        if self._session is None:
            result = self._run(["vagrant", "ssh-config"])
            if result.return_code:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            self._session = SshSession(result.stdout)
        return self._session

    @ssh.deleter
    def ssh(self) -> None:
        if self._session is not None:
            self._session.close()
        self._session = None

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
        del self.ssh

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
        return self._run(self.ssh.command(command), input_text=stdin)

    def push_file(self, local_path: str, remote_path: str) -> None:
        result = self._run(["vagrant", "upload", local_path, remote_path])
        if result.return_code:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    def install_deb(self, deb_path: str) -> CommandResult:
        remote_path = "/tmp/ubuntu-pro-uat.deb"
        self.push_file(deb_path, remote_path)
        result = self.execute(
            "dpkg -i {path}; dpkg_status=$?; "
            "if [ $dpkg_status -ne 0 ]; then "
            "apt-get -f install --yes --no-install-recommends && "
            "dpkg -i {path}; "
            "else exit 0; fi".format(path=shlex.quote(remote_path)),
            use_sudo=True,
        )
        self.execute(["rm", "-f", remote_path], use_sudo=True)
        return result

    def install_package(self, package: str) -> CommandResult:
        return self.execute(
            "apt-get update && "
            "apt-get install --assume-yes --no-install-recommends {}".format(
                shlex.quote(package)
            ),
            use_sudo=True,
        )

    def pull_file(self, remote_path: str, local_path: str) -> None:
        temp_path = "/tmp/vagrant-pull-{}".format(os.getpid())
        result = self.execute(
            "cp {} {} && chmod 0644 {}".format(
                shlex.quote(remote_path),
                shlex.quote(temp_path),
                shlex.quote(temp_path),
            ),
            use_sudo=True,
        )
        if result.return_code:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        result = self._run(self.ssh.download(temp_path, local_path))
        if result.return_code:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        self.execute(["rm", "-f", temp_path])

    def reload(self) -> None:
        result = self._run(["vagrant", "reload"])
        if result.return_code:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        del self.ssh

    def destroy(self) -> None:
        result = self._run(["vagrant", "destroy", "--force"])
        if result.return_code:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        del self.ssh

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
