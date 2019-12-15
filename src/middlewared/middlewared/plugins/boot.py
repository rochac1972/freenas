import os
import platform
import tempfile

from middlewared.schema import Bool, Dict, Int, Str, accepts
from middlewared.service import CallError, Service, job, private
from middlewared.utils import run
from middlewared.validators import Range

try:
    from bsd import geom
except ImportError:
    geom = None


BOOT_POOL_NAME = None
BOOT_POOL_NAME_VALID = ['freenas-boot', 'boot-pool']
IS_LINUX = platform.system().lower() == 'linux'


class BootService(Service):

    @private
    async def pool_name(self):
        return BOOT_POOL_NAME

    @accepts()
    async def get_state(self):
        """
        Returns the current state of the boot pool, including all vdevs, properties and datasets.
        """
        return await self.middleware.call('zfs.pool.query', [('name', '=', BOOT_POOL_NAME)], {'get': True})

    @accepts()
    async def get_disks(self):
        """
        Returns disks of the boot pool.
        """
        return await self.middleware.call('zfs.pool.get_disks', BOOT_POOL_NAME)

    @private
    async def get_boot_type(self):
        """
        Get the boot type of the boot pool.

        Returns:
            "BIOS", "EFI", None
        """
        if IS_LINUX:
            # https://wiki.debian.org/UEFI
            return 'EFI' if os.path.exists('/sys/firmware/efi') else 'BIOS'
        else:
            return self.__get_boot_type_freebsd()

    async def __get_boot_type_freebsd(self):
        await self.middleware.run_in_thread(geom.scan)
        labelclass = geom.class_by_name('PART')
        efi = bios = 0
        for disk in await self.get_disks():
            for e in labelclass.xml.findall(f".//geom[name='{disk}']/provider/config/type"):
                if e.text == 'efi':
                    efi += 1
                elif e.text == 'freebsd-boot':
                    bios += 1
        if efi == 0 and bios == 0:
            return None
        if bios > 0:
            return 'BIOS'
        return 'EFI'

    @private
    async def install_loader(self, boottype, dev):
        if boottype == 'EFI':
            with tempfile.TemporaryDirectory() as tmpdirname:
                await run('mount', '-t', 'msdosfs', f'/dev/{dev}p1', tmpdirname, check=False)
                try:
                    os.makedirs(f'{tmpdirname}/efi/boot')
                except FileExistsError:
                    pass
                await run('cp', '/boot/boot1.efi', f'{tmpdirname}/efi/boot/BOOTx64.efi', check=False)
                await run('umount', tmpdirname, check=False)

        else:
            await run('gpart', 'bootcode', '-b', '/boot/pmbr', '-p', '/boot/gptzfsboot', '-i', '1', f'/dev/{dev}', check=False)

    @accepts(
        Str('dev'),
        Dict(
            'options',
            Bool('expand', default=False),
        ),
    )
    @job(lock='boot_attach')
    async def attach(self, job, dev, options=None):
        """
        Attach a disk to the boot pool, turning a stripe into a mirror.

        `expand` option will determine whether the new disk partition will be
                 the maximum available or the same size as the current disk.
        """

        disks = list(await self.get_disks())
        if len(disks) > 1:
            raise CallError('3-way mirror not supported')

        format_opts = {}
        if not options['expand']:
            # Lets try to find out the size of the current freebsd-zfs partition so
            # the new partition is not bigger, preventing size mismatch if one of
            # them fail later on. See #21336
            disk_parts = await self.middleware.call('disk.list_partitions', disks[0])
            zfs_part_uuid = await self.middleware.call('device.get_zfs_part_type')
            for part in disk_parts:
                if part['partition_type'] == zfs_part_uuid:
                    format_opts['size'] = part['size']

        swap_size = await self.middleware.call('disk.get_swap_size', disks[0])
        if swap_size:
            format_opts['swap_size'] = swap_size
        boottype = await self.middleware.call('boot.format', dev, format_opts)

        pool = await self.middleware.call("zfs.pool.query", [["name", "=", BOOT_POOL_NAME]], {"get": True})

        extend_pool_job = await self.middleware.call('zfs.pool.extend', BOOT_POOL_NAME, None,
                                                     [{'target': pool["groups"]["data"][0]["guid"],
                                                       'type': 'DISK',
                                                       'path': f'/dev/{dev}p2'}])

        await self.install_loader(boottype, dev)

        await job.wrap(extend_pool_job)

        # If the user is upgrading his disks, let's set expand to True to make sure that we
        # register the new disks capacity which increase the size of the pool
        await self.middleware.call('zfs.pool.online', BOOT_POOL_NAME, f'{dev}p2', True)

    @accepts(Str('dev'))
    async def detach(self, dev):
        """
        Detach given `dev` from boot pool.
        """
        await self.middleware.call('zfs.pool.detach', BOOT_POOL_NAME, dev)

    @accepts(Str('label'), Str('dev'))
    async def replace(self, label, dev):
        """
        Replace device `label` on boot pool with `dev`.
        """
        format_opts = {}
        disks = list(await self.get_disks())
        swap_size = await self.middleware.call('disk.get_swap_size', disks[0])
        if swap_size:
            format_opts['swap_size'] = swap_size
        boottype = await self.middleware.call('boot.format', dev, format_opts)
        await self.middleware.call('zfs.pool.replace', BOOT_POOL_NAME, label, f'{dev}p2')
        await self.install_loader(boottype, dev)

    @accepts()
    @job(lock='boot_scrub')
    async def scrub(self, job):
        """
        Scrub on boot pool.
        """
        subjob = await self.middleware.call('zfs.pool.scrub', BOOT_POOL_NAME)
        return await job.wrap(subjob)

    @accepts(
        Int('interval', validators=[Range(min=1)])
    )
    async def set_scrub_interval(self, interval):
        """
        Set Automatic Scrub Interval value in days.
        """
        await self.middleware.call(
            'datastore.update',
            'system.advanced',
            (await self.middleware.call('system.advanced.config'))['id'],
            {'adv_boot_scrub': interval},
        )
        return interval

    @accepts()
    async def get_scrub_interval(self):
        """
        Get Automatic Scrub Interval value in days.
        """
        return (await self.middleware.call('system.advanced.config'))['boot_scrub']


async def setup(middleware):
    global BOOT_POOL_NAME

    pools = (
        await run('zpool', 'list', '-H', '-o', 'name', encoding='utf8')
    ).stdout.strip().split()
    for i in BOOT_POOL_NAME_VALID:
        if i in pools:
            BOOT_POOL_NAME = i
            break
    else:
        middleware.logger.error('Failed to detect boot pool name.')
