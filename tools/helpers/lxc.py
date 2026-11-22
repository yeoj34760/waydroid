# Copyright 2021 Erfan Abdi
# SPDX-License-Identifier: GPL-3.0-or-later
import subprocess
import os
import logging
import glob
import shutil
import signal
import stat
import sys
import time
import platform
import gbinder
import tools.config
import tools.helpers.run
from contextlib import suppress
from pathlib import Path

def get_lxc_version(args):
    if shutil.which("lxc-info") is not None:
        command = ["lxc-info", "--version"]
        version_str = tools.helpers.run.user(args, command, output_return=True)
        return int(version_str[0])
    else:
        return 0

def add_node_entry(nodes, src, dist, mnt_type, options, check):
    if check and not os.path.exists(src):
        return False
    entry = "lxc.mount.entry = "
    entry += src + " "
    if dist is None:
        dist = src[1:]
    entry += dist + " "
    entry += mnt_type + " "
    entry += options
    nodes.append(entry)
    return True

def generate_nodes_lxc_config(args):
    nodes = []
    def make_entry(src, dist=None, mnt_type="none", options="bind,create=file,optional 0 0", check=True):
        return add_node_entry(nodes, src, dist, mnt_type, options, check)

    # Necessary dev nodes
    make_entry("tmpfs", "dev", "tmpfs", "nosuid 0 0", False)
    make_entry("/dev/zero")
    make_entry("/dev/null")
    make_entry("/dev/full")
    make_entry("/dev/ashmem")
    make_entry("/dev/fuse")
    make_entry("/dev/ion")
    make_entry("/dev/tty")
    make_entry("/dev/char", options="bind,create=dir,optional 0 0")

    # Graphic dev nodes
    make_entry("/dev/kgsl-3d0")
    make_entry("/dev/mali0")
    make_entry("/dev/pvr_sync")
    make_entry("/dev/pmsg0")
    make_entry("/dev/dxg")
    render, _ = tools.helpers.gpu.getDriNode(args)
    make_entry(render)

    for n in glob.glob("/dev/fb*"):
        make_entry(n)
    for n in glob.glob("/dev/graphics/fb*"):
        make_entry(n)
    for n in glob.glob("/dev/video*"):
        make_entry(n)
    for n in glob.glob("/dev/dma_heap/*"):
        make_entry(n)

    # Binder dev nodes
    make_entry("/dev/" + args.BINDER_DRIVER, "dev/binder", check=False)
    make_entry("/dev/" + args.VNDBINDER_DRIVER, "dev/vndbinder", check=False)
    make_entry("/dev/" + args.HWBINDER_DRIVER, "dev/hwbinder", check=False)

    if args.vendor_type != "MAINLINE":
        if not make_entry("/dev/hwbinder", "dev/host_hwbinder"):
            raise OSError('Binder node "hwbinder" of host not found')
        make_entry("/vendor", "vendor_extra", options="rbind,optional 0 0")

    # Necessary device nodes for adb
    make_entry("none", "dev/pts", "devpts", "defaults,mode=644,ptmxmode=666,create=dir 0 0", False)
    make_entry("/dev/uhid")

    # TUN/TAP device node for VPN
    make_entry("/dev/net/tun", "dev/tun")

    # Low memory killer sys node
    make_entry("/sys/module/lowmemorykiller", options="bind,create=dir,optional 0 0")

    # Mount host permissions
    make_entry(tools.config.defaults["host_perms"],
               "vendor/etc/host-permissions", options="bind,optional 0 0")

    # Necessary sw_sync node for HWC
    make_entry("/dev/sw_sync")
    make_entry("/sys/kernel/debug", options="rbind,create=dir,optional 0 0")

    # Vibrator
    make_entry("/sys/class/leds/vibrator",
               options="bind,create=dir,optional 0 0")
    make_entry("/sys/devices/virtual/timed_output/vibrator",
               options="bind,create=dir,optional 0 0")

    # Media dev nodes (for Mediatek)
    make_entry("/dev/Vcodec")
    make_entry("/dev/MTK_SMI")
    make_entry("/dev/mdp_sync")
    make_entry("/dev/mtk_cmdq")

    # WSLg
    make_entry("tmpfs", "mnt_extra", "tmpfs", "nodev 0 0", False)
    make_entry("/mnt/wslg", "mnt_extra/wslg",
               options="rbind,create=dir,optional 0 0")

    # Make a tmpfs at every possible rootfs mountpoint
    make_entry("tmpfs", "tmp", "tmpfs", "nodev 0 0", False)
    make_entry("tmpfs", "var", "tmpfs", "nodev 0 0", False)
    make_entry("tmpfs", "run", "tmpfs", "nodev 0 0", False)

    # NFC config
    make_entry("/system/etc/libnfc-nci.conf", options="bind,optional 0 0")

    # NVIDIA Venus stack: host venus socket dir and our replacement guest
    # libraries, bind-mounted over the image. Emitted only in
    # nvidia_driver_mode = venus (socket: nvidia_native.VENUS_SOCKET),
    # and deliberately NOT 'optional': a missing piece must fail container
    # start loudly instead of surfacing as an opaque SurfaceFlinger crash
    # loop deep inside the guest.
    cfg = tools.config.load(args)
    # waydroid-nvidia-prolapse: the driver is chosen explicitly by [waydroid] nvidia_driver_mode
    # (nvidia-prolapse | glibc-shim | venus; nvidia_native.driver_mode() refuses anything ambiguous).
    # venus = exactly the baseline's mounts below. glibc-shim and nvidia-prolapse's dev-root reuse
    # ANGLE, hwcomposer and surfaceflinger from the guest layout below, but bring their own Vulkan
    # HAL and gralloc backend: the Venus-only pieces are not mounted. ANGLE's files are bound by
    # nvidia_native.lxc_nodes() instead, after the staged /vendor/lib{,64}/egl directory that
    # would otherwise hide them. nvidia-prolapse's vendor-image install (driver.guest_layout False)
    # binds nothing from the guest layout: every component is in its vendor.img.
    from tools.helpers import nvidia_native
    driver = nvidia_native.driver_mode(cfg["waydroid"])
    if driver is not None:
        logging.info(nvidia_native.announce(driver))
        nvidia_native.check_properties(cfg["properties"], driver)
    if driver is not None and driver.mode == "venus":
        nvidia_native.check_venus(driver, cfg["waydroid"])
    native = driver is not None and driver.native
    guest_layout = driver is not None and driver.guest_layout
    venus_socket = driver.venus_socket if driver is not None and driver.mode == "venus" else None
    not_in_native = (
        "vendor/lib/hw/vulkan.virtio.so",
        "vendor/lib64/hw/vulkan.virtio.so",
        "vendor/lib64/libgbm_mesa_wrapper.so",
        "vendor/lib/egl/libEGL_angle.so",
        "vendor/lib/egl/libGLESv1_CM_angle.so",
        "vendor/lib/egl/libGLESv2_angle.so",
        "vendor/lib64/egl/libEGL_angle.so",
        "vendor/lib64/egl/libGLESv1_CM_angle.so",
        "vendor/lib64/egl/libGLESv2_angle.so",
    )
    if venus_socket or (native and guest_layout):
        if venus_socket and not native:
            venus_dir = os.path.dirname(venus_socket)
            nodes.append("lxc.mount.entry = {} dev/venus none rbind,create=dir 0 0".format(venus_dir))
        if cfg["waydroid"].get("nvidia_guest_layout") == "2":
            guest_root = "/var/lib/waydroid/nv/guest"
            guest_files = (
                "vendor/lib/hw/vulkan.virtio.so",
                "vendor/lib/egl/libEGL_angle.so",
                "vendor/lib/egl/libGLESv1_CM_angle.so",
                "vendor/lib/egl/libGLESv2_angle.so",
                "vendor/lib64/hw/vulkan.virtio.so",
                "vendor/lib64/egl/libEGL_angle.so",
                "vendor/lib64/egl/libGLESv1_CM_angle.so",
                "vendor/lib64/egl/libGLESv2_angle.so",
                "vendor/lib64/libgbm_mesa_wrapper.so",
                "vendor/lib64/hw/hwcomposer.waydroid.so",
                "system/bin/surfaceflinger",
            )
            for guest_file in guest_files:
                if guest_file in not_in_native and native:
                    continue
                nodes.append(
                    "lxc.mount.entry = {}/{} {} none rbind,create=file 0 0".format(
                        guest_root, guest_file, guest_file))
        elif native:
            raise OSError("NVIDIA native mode reuses ANGLE, hwcomposer and surfaceflinger from "
                          "/var/lib/waydroid/nv/guest and needs nvidia_guest_layout = 2 in "
                          "waydroid.cfg (provisioned by waydroid-nvidia-setup)")
        else:
            # Compatibility with installations provisioned before the dual-ABI
            # guest layout. The next setup run migrates them atomically.
            nodes.append("lxc.mount.entry = /var/lib/waydroid/overlay/vendor/lib64/hw/vulkan.virtio.so vendor/lib64/hw/vulkan.virtio.so none rbind,create=file 0 0")
            nodes.append("lxc.mount.entry = /var/lib/waydroid/nv/libgbm_mesa_wrapper.so vendor/lib64/libgbm_mesa_wrapper.so none rbind,create=file 0 0")
            nodes.append("lxc.mount.entry = /var/lib/waydroid/nv/hwcomposer.waydroid.so vendor/lib64/hw/hwcomposer.waydroid.so none rbind,create=file 0 0")
            nodes.append("lxc.mount.entry = /var/lib/waydroid/nv/libEGL_angle.so vendor/lib64/egl/libEGL_angle.so none rbind,create=file 0 0")
            nodes.append("lxc.mount.entry = /var/lib/waydroid/nv/libGLESv1_CM_angle.so vendor/lib64/egl/libGLESv1_CM_angle.so none rbind,create=file 0 0")
            nodes.append("lxc.mount.entry = /var/lib/waydroid/nv/libGLESv2_angle.so vendor/lib64/egl/libGLESv2_angle.so none rbind,create=file 0 0")
            nodes.append("lxc.mount.entry = /var/lib/waydroid/nv/surfaceflinger system/bin/surfaceflinger none rbind,create=file 0 0")

    # NVIDIA driver modes (waydroid-nvidia-prolapse, nvidia_native.mode_nodes): nvidia-prolapse's
    # vendor image gets the /dev/nvidia* nodes and this GPU's Codec2 listing, after checking that
    # the booted vendor.img is the one the setup gated on the loaded kernel module. glibc-shim and
    # dev-root get their staged root (/vendor_extra, gralloc, the staged /vendor/lib{,64}/egl) and
    # the nodes, and refuse our vendor image. Every entry is mandatory and every missing piece
    # raises with its name and remedy, as does a [properties] section that sets a mode-derived
    # property. Exception: `waydroid init` (args.nvidia_init) runs before waydroid-nvidia-setup has
    # installed the vendor image, so there the vendor-image entries are deferred, not refused
    # (nvidia_native.init_nodes), and start() below refuses until the setup ran.
    if driver is not None and getattr(args, "nvidia_init", False):
        entries, args.nvidia_pending = nvidia_native.init_nodes(driver, cfg["waydroid"], existing=nodes)
        if args.nvidia_pending:
            logging.warning(args.nvidia_pending)
        nodes.extend(entries)
    elif driver is not None:
        nodes.extend(nvidia_native.mode_nodes(driver, cfg["waydroid"], existing=nodes))

    return nodes

LXC_APPARMOR_PROFILE = "lxc-waydroid"
def get_apparmor_status(args):
    enabled = False
    if shutil.which("aa-enabled"):
        enabled = (tools.helpers.run.user(args, ["aa-enabled", "--quiet"], check=False) == 0)
    if not enabled and shutil.which("systemctl"):
        enabled = (tools.helpers.run.user(args, ["systemctl", "is-active", "-q", "apparmor"], check=False) == 0)
    try:
        with open("/sys/kernel/security/apparmor/profiles", "r") as f:
            enabled &= (LXC_APPARMOR_PROFILE in f.read())
    except Exception:
        enabled = False
    return enabled

APPARMOR_KEYS = ("lxc.aa_profile", "lxc.apparmor.profile")

def lxc_parses(config_text):
    """lxc-info's verdict on a container config: (True, "") when this host's LXC loads it, else (False, LXC's
    message). lxc-info parses the config of the container it describes, so a scratch container answers it."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="waydroid-lxc-probe-") as tmp:
        os.mkdir(os.path.join(tmp, "probe"))
        with open(os.path.join(tmp, "probe", "config"), "w") as f:
            f.write(config_text)
        proc = subprocess.run(["lxc-info", "-P", tmp, "-n", "probe"], capture_output=True, text=True,
                              env=dict(os.environ, LC_ALL="C"))
    return proc.returncode == 0, proc.stderr.strip()

def drop_unsupported_apparmor(config_path):
    """An LXC built without AppArmor (Fedora's) refuses lxc.apparmor.profile ("Built without AppArmor support")
    and then cannot load the container at all. Keep the key only when this host's LXC parses it."""
    with open(config_path) as f:
        lines = f.read().splitlines(True)
    keys = [l for l in lines if l.split("=")[0].strip() in APPARMOR_KEYS]
    if not keys:
        return
    ok, message = lxc_parses("")
    if not ok:
        raise OSError("LXC cannot load an empty container config (lxc-info -P <scratch> -n probe): " + message)
    ok, message = lxc_parses("".join(keys))
    if ok:
        return
    logging.info("LXC rejects {} ({}): leaving it out of the container config".format(
        " / ".join(l.strip() for l in keys), message.splitlines()[0] if message else "no message"))
    with open(config_path, "w") as f:
        f.writelines(l for l in lines if l not in keys)

def set_lxc_config(args):
    lxc_path = tools.config.defaults["lxc"] + "/waydroid"
    lxc_ver = get_lxc_version(args)
    if lxc_ver == 0:
        raise OSError("LXC is not installed")
    config_paths = tools.config.tools_src + "/data/configs/config_"
    seccomp_profile = tools.config.tools_src + "/data/configs/waydroid.seccomp"

    config_snippets = [ config_paths + "base" ]
    # lxc v1 and v2 are bit special because some options got renamed later
    if lxc_ver <= 2:
        config_snippets.append(config_paths + "1")
    else:
        for ver in range(3, 5):
            snippet = config_paths + str(ver)
            if lxc_ver >= ver and os.path.exists(snippet):
                config_snippets.append(snippet)

    command = ["mkdir", "-p", lxc_path]
    tools.helpers.run.user(args, command)
    command = ["sh", "-c", "cat {} > \"{}\"".format(' '.join('"{0}"'.format(w) for w in config_snippets), lxc_path + "/config")]
    tools.helpers.run.user(args, command)
    command = ["sed", "-i", "s/LXCARCH/{}/".format(platform.machine()), lxc_path + "/config"]
    tools.helpers.run.user(args, command)
    post_stop_script = tools.config.tools_src + "/data/scripts/waydroid-post-stop.sh"
    command = ["sed", "-i", "s#LXCPOSTSTOP#{}#".format(post_stop_script), lxc_path + "/config"]
    tools.helpers.run.user(args, command)
    command = ["cp", "-fpr", seccomp_profile, lxc_path + "/waydroid.seccomp"]
    tools.helpers.run.user(args, command)
    if get_apparmor_status(args):
        command = ["sed", "-i", "-E", "/lxc.aa_profile|lxc.apparmor.profile/ s/unconfined/{}/g".format(LXC_APPARMOR_PROFILE), lxc_path + "/config"]
        tools.helpers.run.user(args, command)
    drop_unsupported_apparmor(lxc_path + "/config")

    nodes = generate_nodes_lxc_config(args)
    config_nodes_tmp_path = args.work + "/config_nodes"
    with open(config_nodes_tmp_path, "w") as f:
        f.writelines(node + "\n" for node in nodes)
    command = ["mv", config_nodes_tmp_path, lxc_path]
    tools.helpers.run.user(args, command)

    # Create empty file
    Path(os.path.join(lxc_path, "config_session")).touch()

def generate_session_lxc_config(args, session):
    nodes = []
    def make_entry(src, dist=None, mnt_type="none", options="rbind,create=file 0 0"):
        if any(x in src for x in ["\n", "\r"]):
            logging.warning("User-provided mount path contains illegal character: " + src)
            return False
        if dist is None and (not os.path.exists(src) or
                             str(os.stat(src).st_uid) != session["user_id"]):
            logging.warning("User-provided mount path is not owned by user: " + src)
            return False
        return add_node_entry(nodes, src, dist, mnt_type, options, check=False)

    # Make sure XDG_RUNTIME_DIR exists
    if not make_entry("tmpfs", tools.config.defaults["container_xdg_runtime_dir"], options="create=dir 0 0"):
        raise OSError("Failed to create XDG_RUNTIME_DIR mount point")

    wayland_host_socket = os.path.realpath(os.path.join(session["xdg_runtime_dir"], session["wayland_display"]))
    # Container-side path: resolving it on the host gives the wrong one.
    wayland_container_socket = os.path.join(tools.config.defaults["container_xdg_runtime_dir"], tools.config.defaults["container_wayland_display"])
    if not make_entry(wayland_host_socket, wayland_container_socket[1:]):
        raise OSError("Failed to bind Wayland socket")

    # Make sure PULSE_RUNTIME_DIR exists
    pulse_host_socket = os.path.join(session["pulse_runtime_path"], "native")
    pulse_container_socket = os.path.join(tools.config.defaults["container_pulse_runtime_path"], "native")
    make_entry(pulse_host_socket, pulse_container_socket[1:])

    if not make_entry(session["waydroid_data"], "data", options="rbind 0 0"):
        raise OSError("Failed to bind userdata")

    lxc_path = tools.config.defaults["lxc"] + "/waydroid"
    config_nodes_tmp_path = args.work + "/config_session"
    with open(config_nodes_tmp_path, "w") as f:
        f.writelines(node + "\n" for node in nodes)
    command = ["mv", config_nodes_tmp_path, lxc_path]
    tools.helpers.run.user(args, command)

def make_base_props(args):
    def find_hal(hardware):
        hardware_props = [
            "ro.hardware." + hardware,
            "ro.hardware",
            "ro.product.board",
            "ro.arch",
            "ro.board.platform"]
        for p in hardware_props:
            prop = tools.helpers.props.host_get(args, p)
            if prop != "":
                for lib in ["/odm/lib", "/odm/lib64", "/vendor/lib", "/vendor/lib64", "/system/lib", "/system/lib64"]:
                    hal_file = lib + "/hw/" + hardware + "." + prop + ".so"
                    if os.path.isfile(hal_file):
                        return prop
        return ""

    def find_hidl(intf):
        if args.vendor_type == "MAINLINE":
            return False

        try:
            sm = gbinder.ServiceManager("/dev/hwbinder")
            return intf in sm.list_sync()
        except Exception:
            return False

    def find_aidl(intf):
        if args.vendor_type == "MAINLINE":
            return False

        try:
            sm = gbinder.ServiceManager("/dev/binder")
            return intf in sm.list_sync()
        except Exception:
            return False

    props = []

    if not os.path.exists("/dev/ashmem"):
        props.append("sys.use_memfd=true")

    # Added for security reasons
    props.append("ro.adb.secure=1")
    props.append("ro.debuggable=0")

    egl = tools.helpers.props.host_get(args, "ro.hardware.egl")
    dri, _ = tools.helpers.gpu.getDriNode(args)

    gralloc = find_hal("gralloc")
    if not gralloc:
        if find_hidl("android.hardware.graphics.allocator@4.0::IAllocator/default"):
            gralloc = "android"
        elif find_aidl("android.hardware.graphics.allocator.IAllocator/default"):
            gralloc = "android"
    if not gralloc:
        if dri:
            gralloc = "gbm"
            egl = "mesa"
            props.append("gralloc.gbm.device=" + dri)
        else:
            gralloc = "default"
            egl = "swiftshader"
        props.append("debug.stagefright.ccodec=0")
    props.append("ro.hardware.gralloc=" + gralloc)

    if egl != "":
        props.append("ro.hardware.egl=" + egl)

    media_profiles = tools.helpers.props.host_get(args, "media.settings.xml")
    if media_profiles != "":
        media_profiles = media_profiles.replace("vendor/", "vendor_extra/")
        media_profiles = media_profiles.replace("odm/", "odm_extra/")
        props.append("media.settings.xml=" + media_profiles)

    ccodec = tools.helpers.props.host_get(args, "debug.stagefright.ccodec")
    if ccodec != "":
        props.append("debug.stagefright.ccodec=" + ccodec)

    ext_library = tools.helpers.props.host_get(args, "ro.vendor.extension_library")
    if ext_library != "":
        ext_library = ext_library.replace("vendor/", "vendor_extra/")
        ext_library = ext_library.replace("odm/", "odm_extra/")
        props.append("ro.vendor.extension_library=" + ext_library)

    vulkan = find_hal("vulkan")
    if not vulkan and dri:
        vulkan = tools.helpers.gpu.getVulkanDriver(args, os.path.basename(dri))
    if vulkan:
        props.append("ro.hardware.vulkan=" + vulkan)

    treble = tools.helpers.props.host_get(args, "ro.treble.enabled")
    if treble != "true":
        camera = find_hal("camera")
        if camera != "":
            props.append("ro.hardware.camera=" + camera)
        else:
            if args.vendor_type == "MAINLINE":
                props.append("ro.hardware.camera=v4l2")

    opengles = tools.helpers.props.host_get(args, "ro.opengles.version")
    if opengles == "":
        opengles = "196610"
    props.append("ro.opengles.version=" + opengles)

    # Some Mali devices require ro.vendor.arm.egl.* props from the
    # host for libEGL/GLES to work
    for k, v in tools.helpers.props.host_list(args, "ro.vendor.arm.egl.").items():
        props.append(k + "=" + v)

    if args.images_path not in tools.config.defaults["preinstalled_images_paths"]:
        props.append("waydroid.system_ota=" + args.system_ota)
        props.append("waydroid.vendor_ota=" + args.vendor_ota)
    else:
        props.append("waydroid.updater.disabled=true")

    props.append("waydroid.tools_version=" + tools.config.version)

    if args.vendor_type == "MAINLINE":
        props.append("ro.vndk.lite=true")

    for product in ["brand", "device", "manufacturer", "model", "name"]:
        prop_product = tools.helpers.props.host_get(
            args, "ro.product.vendor." + product)
        if prop_product != "":
            props.append("ro.product.waydroid." + product + "=" + prop_product)
        else:
            if os.path.isfile("/proc/device-tree/" + product):
                with open("/proc/device-tree/" + product) as f:
                    f_value = f.read().strip().rstrip('\x00')
                    if f_value != "":
                        props.append("ro.product.waydroid." +
                                     product + "=" + f_value)

    prop_fp = tools.helpers.props.host_get(args, "ro.vendor.build.fingerprint")
    if prop_fp != "":
        props.append("ro.build.fingerprint=" + prop_fp)

    # waydroid-nvidia-prolapse: properties derived from nvidia_driver_mode/nvidia_native_gl and the
    # deployment (ro.hardware.vulkan/egl, zygote preload, Venus socket, the Codec2 listing of a root
    # with the NVDEC decoder: nvidia_native.mode_properties). They replace Waydroid's own values
    # above; [properties] may not set them (check_properties).
    cfg = tools.config.load(args)
    from tools.helpers import nvidia_native
    driver = nvidia_native.driver_mode(cfg["waydroid"])
    if driver is not None:
        nvidia_native.check_properties(cfg["properties"], driver)
        props = nvidia_native.apply_mode_properties(props, driver)

    # now append/override with values in [properties] section of waydroid.cfg
    for k, v in cfg["properties"].items():
        for idx, elem in enumerate(props):
            if (k+"=") in elem:
                props.pop(idx)
        props.append(k+"="+v)

    with open(args.work + "/waydroid_base.prop", "w") as f:
        f.writelines(prop + "\n" for prop in props)


def setup_host_perms(args):
    if not os.path.exists(tools.config.defaults["host_perms"]):
        os.mkdir(tools.config.defaults["host_perms"])

    treble = tools.helpers.props.host_get(args, "ro.treble.enabled")
    if treble != "true":
        return

    sku = tools.helpers.props.host_get(args, "ro.boot.product.hardware.sku")
    copy_list = []
    copy_list.extend(
        glob.glob("/vendor/etc/permissions/android.hardware.nfc.*"))
    if os.path.exists("/vendor/etc/permissions/android.hardware.consumerir.xml"):
        copy_list.append("/vendor/etc/permissions/android.hardware.consumerir.xml")
    copy_list.extend(
        glob.glob("/odm/etc/permissions/android.hardware.nfc.*"))
    if os.path.exists("/odm/etc/permissions/android.hardware.consumerir.xml"):
        copy_list.append("/odm/etc/permissions/android.hardware.consumerir.xml")
    if sku != "":
        copy_list.extend(
            glob.glob("/odm/etc/permissions/sku_{}/android.hardware.nfc.*".format(sku)))
        if os.path.exists("/odm/etc/permissions/sku_{}/android.hardware.consumerir.xml".format(sku)):
            copy_list.append(
                "/odm/etc/permissions/sku_{}/android.hardware.consumerir.xml".format(sku))

    for filename in copy_list:
        shutil.copy(filename, tools.config.defaults["host_perms"])

def status(args):
    command = ["lxc-info", "-P", tools.config.defaults["lxc"], "-n", "waydroid", "-sH"]
    try:
        return tools.helpers.run.user(args, command, output_return=True).strip()
    except Exception:
        logging.info("Couldn't get LXC status. Assuming STOPPED.")
        return "STOPPED"

def wait_for_running(args):
    lxc_status = status(args)
    timeout = 10
    while lxc_status != "RUNNING" and timeout > 0:
        lxc_status = status(args)
        logging.info(
            "waiting {} seconds for container to start...".format(timeout))
        timeout = timeout - 1
        time.sleep(1)
    if lxc_status != "RUNNING":
        raise OSError("container failed to start")

def start(args):
    # waydroid-nvidia-prolapse: name the NVIDIA driver mode in the container log, refuse a
    # waydroid_base.prop generated for another mode (cfg switched without 'upgrade -o'), and in the
    # vendor-image install refuse a vendor.img other than the gated one, or the entries `waydroid
    # init` deferred before the setup installed it (nvidia_native.check_start).
    from tools.helpers import nvidia_native
    waydroid_cfg = tools.config.load(args)["waydroid"]
    driver = nvidia_native.driver_mode(waydroid_cfg)
    if driver is not None:
        logging.info(nvidia_native.announce(driver))
        nvidia_native.check_base_prop(args.work + "/waydroid_base.prop", driver)
        nvidia_native.check_start(driver, waydroid_cfg,
                                  os.path.join(tools.config.defaults["lxc"], "waydroid", "config_nodes"))
    command = ["lxc-start", "-P", tools.config.defaults["lxc"],
               "-F", "-n", "waydroid", "--", "/init"]
    tools.helpers.run.user(args, command, output="background")
    wait_for_running(args)
    # Workaround lxc-start changing stdout/stderr permissions to 700
    with suppress(OSError):
        os.chmod(args.log, 0o666)

def stop(args):
    command = ["lxc-stop", "-P",
               tools.config.defaults["lxc"], "-n", "waydroid", "-k"]
    tools.helpers.run.user(args, command)

def freeze(args):
    command = ["lxc-freeze", "-P", tools.config.defaults["lxc"], "-n", "waydroid"]
    tools.helpers.run.user(args, command)

def unfreeze(args):
    command = ["lxc-unfreeze", "-P",
               tools.config.defaults["lxc"], "-n", "waydroid"]
    tools.helpers.run.user(args, command)

ANDROID_ENV = {
    "PATH": "/product/bin:/apex/com.android.runtime/bin:/apex/com.android.art/bin:/system_ext/bin:/system/bin:/system/xbin:/odm/bin:/vendor/bin:/vendor/xbin",
    "ANDROID_ROOT": "/system",
    "ANDROID_DATA": "/data",
    "ANDROID_STORAGE": "/storage",
    "ANDROID_ART_ROOT": "/apex/com.android.art",
    "ANDROID_I18N_ROOT": "/apex/com.android.i18n",
    "ANDROID_TZDATA_ROOT": "/apex/com.android.tzdata",
    "ANDROID_RUNTIME_ROOT": "/apex/com.android.runtime",
    "BOOTCLASSPATH": "/apex/com.android.art/javalib/core-oj.jar:/apex/com.android.art/javalib/core-libart.jar:/apex/com.android.art/javalib/core-icu4j.jar:/apex/com.android.art/javalib/okhttp.jar:/apex/com.android.art/javalib/bouncycastle.jar:/apex/com.android.art/javalib/apache-xml.jar:/system/framework/framework.jar:/system/framework/ext.jar:/system/framework/telephony-common.jar:/system/framework/voip-common.jar:/system/framework/ims-common.jar:/system/framework/framework-atb-backward-compatibility.jar:/apex/com.android.conscrypt/javalib/conscrypt.jar:/apex/com.android.media/javalib/updatable-media.jar:/apex/com.android.mediaprovider/javalib/framework-mediaprovider.jar:/apex/com.android.os.statsd/javalib/framework-statsd.jar:/apex/com.android.permission/javalib/framework-permission.jar:/apex/com.android.sdkext/javalib/framework-sdkextensions.jar:/apex/com.android.wifi/javalib/framework-wifi.jar:/apex/com.android.tethering/javalib/framework-tethering.jar"
}

def android_env_attach_options(args):
    local_env = ANDROID_ENV.copy()
    # Include CLASSPATH env that was generated by Android
    command = ["lxc-attach", "-P", tools.config.defaults["lxc"],
               "-n", "waydroid", "--clear-env", "--",
               "/system/bin/cat" ,"/data/system/environ/classpath"]
    allowed = ["CLASSPATH", "SYSTEMSERVER"]
    with suppress(Exception):
        p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        out, _ = p.communicate()
        if p.returncode == 0:
            for line in out.decode().splitlines():
                _, k, v = line.split(' ', 2)
                if any(pattern in k for pattern in allowed):
                    local_env[k] = v
    env = [k + "=" + v for k, v in local_env.items()]
    return [x for var in env for x in ("--set-var", var)]

def shell(args):
    state = status(args)
    if state == "FROZEN":
        unfreeze(args)
    elif state != "RUNNING":
        logging.error("WayDroid container is {}".format(state))
        return
    command = ["lxc-attach", "-P", tools.config.defaults["lxc"],
               "-n", "waydroid", "--clear-env"]
    command.extend(android_env_attach_options(args))
    if args.uid is not None:
        command.append("--uid="+str(args.uid))
    if args.gid is not None:
        command.append("--gid="+str(args.gid))
    elif args.uid is not None:
        command.append("--gid="+str(args.uid))
    if args.nolsm or args.allcaps or args.nocgroup:
        elevatedprivs = "--elevated-privileges="
        addpipe = False
        if args.nolsm:
            if addpipe:
                elevatedprivs+="|"
            elevatedprivs+="LSM"
            addpipe = True
        if args.allcaps:
            if addpipe:
                elevatedprivs+="|"
            elevatedprivs+="CAP"
            addpipe = True
        if args.nocgroup:
            if addpipe:
                elevatedprivs+="|"
            elevatedprivs+="CGROUP"
            addpipe = True
        command.append(elevatedprivs)
    if args.context is not None and not args.nolsm:
        command.append("--context="+args.context)
    command.append("--")
    if args.COMMAND:
        command.extend(args.COMMAND)
    else:
        command.append("/system/bin/sh")

    def mock_sigint(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, mock_sigint)
    signal.signal(signal.SIGHUP, mock_sigint)

    perms = stat.S_IMODE(os.stat(sys.stdout.fileno()).st_mode)
    try:
        subprocess.run(command)
    except KeyboardInterrupt:
        pass
    finally:
        os.chmod(sys.stdout.fileno(), perms)

    if state == "FROZEN":
        freeze(args)

def logcat(args):
    args.COMMAND = ["/system/bin/logcat"]
    if args.ARGS:
        args.COMMAND.extend(args.ARGS)
    args.uid = None
    args.gid = None
    args.nolsm = None
    args.allcaps = None
    args.nocgroup = None
    args.context = None
    shell(args)
