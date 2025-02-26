#!/usr/bin/env python3
# -*- mode: python -*-

import os
import sys
import glob
import base64
import hashlib
import shutil
import tempfile
from enum import Enum
from argparse import ArgumentParser
from s3backup import S3Config
from pbench import init_report_template, report_status, rename_tb_link, \
    PbenchConfig, BadConfig, get_es, get_pbench_logger, quarantine, md5sum
from botocore.exceptions import ConnectionClosedError, ClientError

_NAME_ = "pbench-backup-tarballs"

# The link source and destination for this operation of this script.
_linksrc = "TO-BACKUP"
_linklarge = "TO-BACKUP-LARGE"
_linkdest = "BACKED-UP"


class Status(Enum):
    SUCCESS = 0
    FAIL = 1
    TOO_LARGE = 2


class LocalBackupObject(object):
    def __init__(self, config):
        self.backup_dir = config.BACKUP
        self.qdir = config.QDIR


class Results(object):
    def __init__(self, ntotal=0, nbackup_success=0, nbackup_fail=0,
                 ns3_success=0, ns3_fail=0, nquaran=0, ns3_toolarge=0):
        self.ntotal = ntotal
        self.nbackup_success = nbackup_success
        self.nbackup_fail = nbackup_fail
        self.ns3_success = ns3_success
        self.ns3_fail = ns3_fail
        self.nquaran = nquaran
        self.ns3_toolarge = ns3_toolarge


def sanity_check(lb_obj, s3_obj, config, logger):
    # make sure archive is present
    archive = config.ARCHIVE

    if not os.path.realpath(archive):
        logger.error(
            'The ARCHIVE directory {}, does not resolve to a real location'.format(archive))
        return None, None

    if not os.path.isdir(archive):
        logger.error(
            'The ARCHIVE directory {}, does not resolve {} to a directory'.format(archive, os.path.realpath(archive)))
        return None, None

    # make sure the local backup directory is present
    backup = config.BACKUP

    if len(backup) == 0:
        logger.error(
            'Unspecified backup directory, no pbench-backup-dir config in pbench-server section')
        lb_obj = None

    try:
        os.mkdir(backup)
    except FileExistsError:
        # directory already exists, verify it
        if not os.path.realpath(backup):
            logger.error(
                'The BACKUP directory {}, does not resolve to a real location'.format(backup))
            lb_obj = None

        if not os.path.isdir(backup):
            logger.error(
                'The BACKUP directory {}, does not resolve {} to a directory'.format(backup, os.path.realpath(backup)))
            lb_obj = None
    except Exception:
        logger.exception(
            "os.mkdir: Unable to create backup destination directory: {}".format(backup))
        lb_obj = None

    # make sure the quarantine directory is present
    qdir = config.QDIR

    if len(qdir) == 0:
        logger.error(
            'Unspecified quarantine directory, no pbench-quarantine-dir config in pbench-server section')
        lb_obj = None

    if not os.path.realpath(qdir):
        logger.error(
            'The QUARANTINE directory {}, does not resolve to a real location'.format(qdir))
        lb_obj = None

    if not os.path.isdir(qdir):
        logger.error(
            'The QUARANTINE directory {}, does not resolve {} to a directory'.format(qdir, os.path.realpath(qdir)))
        lb_obj = None

    # make sure the S3 bucket exists
    try:
        s3_obj.connector.head_bucket(Bucket='{}'.format(s3_obj.bucket_name))
    except Exception:
        logger.exception(
            "Bucket: {} does not exist or you have no access".format(s3_obj.bucket_name))
        s3_obj = None

    return (lb_obj, s3_obj)


def backup_to_local(lb_obj, logger, controller_path, controller, tb, tar, resultname, archive_md5, archive_md5_hex_value):
    if lb_obj is None:
        # Short-circuit operation when we don't have an lb object. This can
        # happen when the expected result of sanity check does not exist, or
        # for other errors where we still want to backup in S3.
        return Status.FAIL

    backup_controller_path = os.path.join(lb_obj.backup_dir, controller)

    # make sure the controller is present in local backup directory
    try:
        os.mkdir(backup_controller_path)
    except FileExistsError:
        # directory already exists, ignore
        pass
    except Exception:
        logger.exception(
            "os.mkdir: Unable to create backup destination directory: {}".format(backup_controller_path))
        return Status.FAIL

    # Check if tarball exists in local backup
    backup_tar = os.path.join(backup_controller_path, resultname)
    if os.path.exists(backup_tar) and os.path.isfile(backup_tar):
        backup_md5 = os.path.join(
            backup_controller_path, "{}.md5".format(resultname))

        # check backup md5 file exist and it is a regular file
        if os.path.exists(backup_md5) and os.path.isfile(backup_md5):
            pass
        else:
            # backup md5 file does not exist or it is not a regular file
            logger.error(
                "{md5} does not exist or it is not a regular file".format(md5=backup_md5))
            return Status.FAIL

        # read backup md5 file
        try:
            with open(backup_md5) as f:
                backup_md5_hex_value = f.readline().split(" ")[0]
        except Exception:
            # Could not read file
            logger.exception(
                "Could not read file {md5}".format(md5=backup_md5))
            return Status.FAIL
        else:
            if archive_md5_hex_value == backup_md5_hex_value:
                # declare success
                logger.info(
                    "Already locally backed-up: {tarball}".format(tarball=os.path.join(controller, resultname)))
                return Status.SUCCESS
            else:
                # md5 file of archive and backup does not match
                logger.error(
                    "{tarball} already exists in backup but md5 sums of archive and backup disagree".format(
                        tarball=os.path.join(controller, resultname)))
                return Status.FAIL
    else:
        tar_done = False

        # copy the md5 file from archive to backup
        try:
            shutil.copy(archive_md5, backup_controller_path)
        except Exception:
            # couldn't copy md5 file
            md5_done = False
            logger.exception(
                "shutil.copy: Unable to copy {} from archive to backup: {}".format(
                    archive_md5, backup_controller_path))
        else:
            md5_done = True

        # copy the tarball from archive to backup
        if md5_done:
            try:
                shutil.copy(tar, backup_controller_path)
            except Exception:
                # couldn't copy tarball
                tar_done = False
                logger.exception(
                    "shutil.copy: Unable to copy {} from archive to backup: {}".format(
                        tar, backup_controller_path))

                # remove the copied md5 file from backup
                bmd5_file = os.path.join(
                    backup_controller_path, "{}.md5".format(resultname))
                if os.path.exists(bmd5_file):
                    try:
                        os.remove(bmd5_file)
                    except Exception:
                        logger.exception("Unable to remove: {}".format(bmd5_file))
            else:
                tar_done = True

        if md5_done and tar_done:
            logger.info(
                "Locally backed-up sucessfully: {tarball}".format(tarball=os.path.join(controller, resultname)))
            return Status.SUCCESS
        else:
            return Status.FAIL


def backup_to_s3(s3_obj, logger, controller_path, controller, tb, tar, resultname, archive_md5_hex_value):
    if s3_obj is None:
        # Short-circuit operation when we don't have an S3 object to work with
        # when executing.  This can happen when the expected bucket does not
        # exist, or for other errors where we still want to backup locally.
        return Status.FAIL

    s3_resultname = os.path.join(controller, resultname)

    # Check if the result already present in s3 or not
    try:
        obj = s3_obj.connector.get_object(Bucket='{}'.format(
            s3_obj.bucket_name), Key='{}'.format(s3_resultname))
    except Exception:
        s3_md5 = None
    else:
        s3_md5 = obj['ETag'].strip("\"")

    if s3_md5 is not None:
        # compare md5 which we already have so no need to recalculate
        if archive_md5_hex_value == s3_md5:
            # declare success
            logger.info(
                "The tarball {key} is already present in S3 bucket with same md5".format(key=s3_resultname))
            _status = Status.SUCCESS
        else:
            logger.error(
                "The tarball {key} is already present in S3 bucket, but with different MD5".format(key=s3_resultname))
            _status = Status.FAIL
        return _status

    tb_size = s3_obj.connector.getsize(tar)
    if tb_size > (5 * (1024 ** 3)):
        # FIXME: We will eventually implement multipart objects to take care
        # of this but for now, we return a special failure so that the link is
        # moved aside and we don't retry, but we save the links for eventual
        # stashing into S3. Verification will complain about these.
        logger.warning("Tar ball, {}, too large to upload", tar)
        return Status.TOO_LARGE

    md5_base64_value = (base64.b64encode(
        bytes.fromhex(archive_md5_hex_value))).decode()
    try:
        with open(tar, 'rb') as data:
            try:
                s3_obj.connector.put_object(
                    Bucket=s3_obj.bucket_name, Key=s3_resultname, Body=data, ContentMD5=md5_base64_value)
            except ConnectionClosedError:
                # This is a transient failure and will be retried at the next
                # invocation of the backups.
                logger.error(
                    "Upload to s3 failed, connection was reset while transferring {key}".format(
                        key=s3_resultname))
                return Status.FAIL
            except Exception:
                # What ever the reason is for this failure, the operation will
                # be retried the next time backups are run.
                logger.exception(
                    "Upload to S3 failed while transferring {key}".format(
                        key=s3_resultname))
                return Status.FAIL
            else:
                logger.info(
                    "Upload to s3 succeeded: {key}".format(key=s3_resultname))
                return Status.SUCCESS
    except Exception:
        # could not read tarball
        logger.exception(
            "Failed to open tarball {tarball}".format(tarball=tar))
        return Status.FAIL


def backup_data(lb_obj, s3_obj, config, logger):
    qdir = config.QDIR

    tarlist = glob.iglob(os.path.join(config.ARCHIVE, '*', _linksrc, '*.tar.xz'))
    ntotal = nbackup_success = nbackup_fail = \
        ns3_success = ns3_fail = nquaran = ns3_toolarge = 0

    for tb in sorted(tarlist):
        ntotal += 1
        # resolve the link
        tar = os.path.realpath(tb)

        # check tarball exist and it is a regular file
        if os.path.exists(tar) and os.path.isfile(tar):
            pass
        else:
            # tarball does not exist or it is not a regular file
            quarantine(qdir, logger, tb)
            nquaran += 1
            logger.error(
                "Quarantine: {}, {} does not exist or it is not a regular file".format(tb, tar))
            continue

        archive_md5 = ("{}.md5".format(tar))

        # check md5 file exist and it is a regular file
        if os.path.exists(archive_md5) and os.path.isfile(archive_md5):
            pass
        else:
            # md5 file does not exist or it is not a regular file
            quarantine(qdir, logger, tb)
            nquaran += 1
            logger.error(
                "Quarantine: {}, {} does not exist or it is not a regular file".format(tb, archive_md5))
            continue

        # read the md5sum from md5 file
        try:
            with open(archive_md5) as f:
                archive_md5_hex_value = f.readline().split(" ")[0]
        except Exception:
            # Could not read file.
            quarantine(qdir, logger, tb)
            nquaran += 1
            logger.exception(
                "Quarantine: {}, Could not read {}".format(tb, archive_md5))
            continue

        # match md5sum of the tarball to its md5 file
        try:
            archive_tar_hex_value = md5sum(tar)
        except Exception:
            # Could not read file.
            quarantine(qdir, logger, tb)
            nquaran += 1
            logger.exception(
                "Quarantine: {}, Could not read {}".format(tb, tar))
            continue

        if archive_tar_hex_value != archive_md5_hex_value:
            quarantine(qdir, logger, tb)
            nquaran += 1
            logger.error(
                "Quarantine: {}, md5sum of {} does not match with its md5 file {}".format(tb, tar, archive_md5))
            continue

        resultname = os.path.basename(tar)
        controller_path = os.path.dirname(tar)
        controller = os.path.basename(controller_path)

        # This function call will handle all the local backup related
        # operations and count the number of successes and failures.
        local_backup_result = backup_to_local(
            lb_obj, logger, controller_path, controller, tb, tar, resultname, archive_md5, archive_md5_hex_value)

        if local_backup_result == Status.SUCCESS:
            nbackup_success  += 1
        elif local_backup_result == Status.FAIL:
            nbackup_fail += 1
        else:
            assert False, "Impossible situation, local_backup_result = {!r}".format(
                local_backup_result)

        # This function call will handle all the S3 bucket related operations
        # and count the number of successes and failures.
        s3_backup_result = backup_to_s3(
            s3_obj, logger, controller_path, controller, tb, tar, resultname, archive_md5_hex_value)

        if s3_backup_result == Status.SUCCESS:
            ns3_success += 1
        elif s3_backup_result == Status.FAIL:
            ns3_fail += 1
        elif s3_backup_result == Status.TOO_LARGE:
            ns3_toolarge += 1
        else:
            assert False, "Impossible situation, s3_backup_result = {!r}".format(
                s3_backup_result)

        if local_backup_result == Status.SUCCESS \
                and s3_backup_result == Status.SUCCESS:
            # Move tar ball symlink to its final resting place
            rename_tb_link(tb, os.path.join(
                controller_path, _linkdest), logger)
        elif local_backup_result == Status.SUCCESS \
                and s3_backup_result == Status.TOO_LARGE:
            # Move tar ball symlink to indicate we still need to deal with all
            # the tar balls that are too large.
            rename_tb_link(tb, os.path.join(
                controller_path, _linklarge), logger)
        else:
            # Do nothing when the backup fails, allowing us to retry on a
            # future pass.
            pass

    return Results(ntotal=ntotal,
                   nbackup_success=nbackup_success,
                   nbackup_fail=nbackup_fail,
                   ns3_success=ns3_success,
                   ns3_fail=ns3_fail,
                   nquaran=nquaran,
                   ns3_toolarge=ns3_toolarge)


def main():
    cfg_name = os.environ.get("CONFIG")

    if not cfg_name:
        print("{}: ERROR: No config file specified; set CONFIG env variable or"
              " use --config <file> on the command line".format(_NAME_),
              file=sys.stderr)
        return 2

    try:
        config = PbenchConfig(cfg_name)
    except BadConfig as e:
        print("{}: {}".format(_NAME_, e), file=sys.stderr)
        return 1

    logger = get_pbench_logger(_NAME_, config)

    # Add a BACKUP and QDIR field to the config object
    config.BACKUP = config.conf.get("pbench-server", "pbench-backup-dir")
    config.QDIR = config.get('pbench-server', 'pbench-quarantine-dir')

    # call the LocalBackupObject class
    lb_obj = LocalBackupObject(config)

    # call the S3Config class
    s3_obj = S3Config(config)

    lb_obj, s3_obj = sanity_check(lb_obj, s3_obj, config, logger)

    if lb_obj is None and s3_obj is None:
        return 3

    logger.info('start-{}'.format(config.TS))

    # Initiate the backup
    counts = backup_data(lb_obj, s3_obj, config, logger)

    result_string = ("Total processed: {},"
                     " Local backup successes: {},"
                     " Local backup failures: {},"
                     " S3 upload successes: {},"
                     " SE upload too large: {},"
                     " S3 upload failures: {},"
                     " Quarantined: {}"
                     .format(counts.ntotal,
                             counts.nbackup_success,
                             counts.nbackup_fail,
                             counts.ns3_success,
                             counts.ns3_toolarge,
                             counts.ns3_fail,
                             counts.nquaran))

    logger.info(result_string)

    prog = os.path.basename(sys.argv[0])

    # prepare and send report
    with tempfile.NamedTemporaryFile(mode='w+t', dir=config.TMP) as report:
        report.write("{}.{}({})\n{}\n".format(
            prog, config.timestamp(), config.PBENCH_ENV, result_string))
        report.seek(0)

        es, idx_prefix = init_report_template(config, logger)
        # Call report-status
        report_status(es, logger, config.LOGSDIR,
                      idx_prefix, _NAME_, config.timestamp(), "status", report.name)

    logger.info('end-{}'.format(config.TS))

    return 0


if __name__ == '__main__':
    status = main()
    sys.exit(status)
