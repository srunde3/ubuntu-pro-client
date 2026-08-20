import os

from driver.vagrant_machine import VagrantMachine


class Config:
    def __init__(self):
        self.keep_vm = os.getenv("UACLIENT_UAT_KEEP_VM", "0") == "1"
        self.vagrant_dir = os.path.dirname(os.path.dirname(__file__))
        self.deb_path = os.getenv("UACLIENT_UAT_DEB_PATH")


def before_all(context):
    context.uat_config = Config()
    context.machines = {}


def before_scenario(context, scenario):
    context.machine = None


def after_scenario(context, scenario):
    pass


def after_all(context):
    if not context.machines or context.uat_config.keep_vm:
        return
    for machine in context.machines.values():
        machine.destroy()


def get_machine(context):
    if getattr(context, "machine", None) is None:
        if not context.uat_config.deb_path:
            raise RuntimeError(
                "Set UACLIENT_UAT_DEB_PATH before running the smoke suite"
            )
        context.machine = VagrantMachine(context.uat_config.vagrant_dir)
        context.machine.up()
        context.machines["SUT"] = context.machine
        install_deb(context)
    return context.machine


def install_deb(context):
    deb_path = context.uat_config.deb_path
    if not deb_path:
        raise RuntimeError(
            "Set UACLIENT_UAT_DEB_PATH to the local ubuntu-pro-client .deb"
        )
    if not os.path.isfile(deb_path):
        raise RuntimeError("Debian package does not exist: {}".format(deb_path))
    result = context.machine.install_deb(deb_path)
    if result.return_code:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
