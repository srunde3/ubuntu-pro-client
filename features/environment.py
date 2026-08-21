import os

from features.driver.vagrant_machine import VagrantMachine

PREBUILT = "prebuilt"
ARCHIVE = "archive"
INSTALL_MODES = (PREBUILT, ARCHIVE)
CLIENT_PACKAGE = "ubuntu-advantage-tools"


class Config:
    def __init__(self):
        self.keep_vm = os.getenv("UACLIENT_UAT_KEEP_VM", "0") == "1"
        self.vagrant_dir = os.path.dirname(__file__)
        self.deb_path = os.getenv("UACLIENT_UAT_DEB_PATH")
        self.contract_token = os.getenv("UACLIENT_UAT_CONTRACT_TOKEN")
        self.install_from = os.getenv(
            "UACLIENT_UAT_INSTALL_FROM", PREBUILT if self.deb_path else ARCHIVE
        )
        if self.install_from not in INSTALL_MODES:
            raise RuntimeError(
                "UACLIENT_UAT_INSTALL_FROM must be one of {}, got {!r}".format(
                    ", ".join(INSTALL_MODES), self.install_from
                )
            )
        if self.install_from == PREBUILT and not self.deb_path:
            raise RuntimeError(
                "UACLIENT_UAT_DEB_PATH is required when installing from a "
                "prebuilt package"
            )


def before_all(context):
    context.uat_config = Config()
    context.machines = {}


def _destroy_machines(context):
    for machine in context.machines.values():
        machine.destroy()
    context.machines.clear()


def before_feature(context, feature):
    context.machine = None
    _destroy_machines(context)
    VagrantMachine(context.uat_config.vagrant_dir).destroy()


def before_scenario(context, scenario):
    context.machine = context.machines.get("SUT")


def after_scenario(context, scenario):
    pass


def after_feature(context, feature):
    if not context.uat_config.keep_vm:
        _destroy_machines(context)
    context.machine = None


def after_all(context):
    if not context.machines or context.uat_config.keep_vm:
        return
    _destroy_machines(context)


def get_machine(context):
    if getattr(context, "machine", None) is None:
        context.machine = VagrantMachine(context.uat_config.vagrant_dir)
        context.machine.up()
        context.machines["SUT"] = context.machine
        install_client(context)
    return context.machine


def install_client(context):
    if context.uat_config.install_from == ARCHIVE:
        result = context.machine.install_package(CLIENT_PACKAGE)
    else:
        deb_path = context.uat_config.deb_path
        if not os.path.isfile(deb_path):
            raise RuntimeError("Debian package does not exist: {}".format(deb_path))
        result = context.machine.install_deb(deb_path)
    if result.return_code:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
