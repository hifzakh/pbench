#! /bin/bash
# -*- mode: shell-script -*-

# This script is the first part of the pipeline that processes pbench
# results tarballs.

# First stage:  pbench-unpack-tarballs looks in all the TODO
#               directories, unpacks tarballs, checks MD5 sums and
#               moves the symlink from the TODO subdir to the
#               TO-COPY-SOS subdir.  It runs under cron once a minute
#               in order to minimize the delay between uploading the
#               results and making them available for viewing over the
#               web.

# Second stage: pbench-copy-sosreports looks in all the TO-COPY-SOS
#               subdirs, extracs the sos report from the tarball and
#               copies it to the VoS incoming area for further
#               processing. Assuming that all is well, it moves the
#               symlink from the TO-COPY-SOS subdir to the TO-INDEX
#               subdir.

# Third stage:  pbench-index looks in all the TO-INDEX subdirs and
#               calls the pbench-results-indexer script to index the
#               results tarball into ES. It then moves the symlink from
#               the TO-INDEX subdir to the DONE subdir.

# assumptions:
# - this script runs as a cron job
# - tarballs and md5 sums are uploaded by move/copy-results to
#   $ARCHIVE/$(hostname -s) area.
# - move/copy-results also makes a symlink to each tarball it uploads
#   in $ARCHIVE/TODO.

# This script loops over the contents of $archive/TODO, verifies the md5
# sum of each tarball, and if correct, it unpacks the tarball into
# .../incoming/$(hostname -s)/.  If everything works, it then moves the
# symlink from $ARCHIVE/TODO to $ARCHIVE/TO-COPY-SOS.


# load common things
. $dir/pbench-base.sh

# check that all the directories exist
test -d $ARCHIVE || doexit "Bad ARCHIVE=$ARCHIVE"
test -d $INCOMING || doexit "Bad INCOMING=$INCOMING"
test -d $RESULTS || doexit "Bad RESULTS=$RESULTS"
test -d $USERS || doexit "Bad USERS=$USERS"

# make sure only one copy is running.
# Use 'flock -n $LOCKFILE /home/pbench/bin/pbench-unpack-tarballs' in the
# crontab to ensure that only one copy is running. The script itself
# does not use any locking.

# the link source and destination(s) for this script
linksrc=TO-UNPACK
linkdest=UNPACKED
linkdestlist=$(getconf.py -l unpacked-states pbench-server)

tmp=$TMP/$PROG.$$
trap 'rm -rf $tmp' EXIT INT QUIT

mkdir -p $tmp || doexit "Failed to create $tmp"

log_init $PROG

echo $TS

# Accumulate errors and logs in files for reporting at the end.
mail_content=$tmp/mail_content.log
index_content=$tmp/index_mail_contents

# get the list of files we'll be operating on - sort them by size
list=$tmp/$PROG.list

# Find all the links in all the $ARCHIVE/<controller>/$linksrc
# directories, emitting a list of their full paths with the size
# in bytes of the file the link points to, and then sort them so
# that we process the smallest tar balls first.
> ${list}.unsorted
# First we find all the $linksrc directories
for linksrc_dir in $(find $ARCHIVE/ -maxdepth 2 -type d -name $linksrc); do
    # Find all the links in a given $linksrc directory that are
    # links to actual files (bad links are not emitted!).
    find -L $linksrc_dir -type f -name '*.tar.xz' -printf "%s %p\n" 2>/dev/null >> ${list}.unsorted
    # Find all the links in the same $linksrc directory that don't
    # link to anything so that we can count them as errors below.
    find -L $linksrc_dir -type l -name '*.tar.xz' -printf "%s %p\n" 2>/dev/null >> ${list}.unsorted
done
sort -n ${list}.unsorted > ${list}
rm -f ${list}.unsorted

typeset -i ntb=0
typeset -i ntotal=0
typeset -i nerrs=0
typeset -i ndups=0
typeset -i nwarn=0

# Initialize report content
> $mail_content
> $index_content

while read size result ;do
    ntotal=$ntotal+1

    link=$(readlink -e $result)
    if [ -z "$link" ] ;then
        echo "$TS: symlink target for $result does not exist" | tee -a $mail_content >&4
        nerrs=$nerrs+1
        # FIXME: quarantine $result
        continue
    fi
    resultname=$(basename $result)
    resultname=${resultname%.tar.xz}

    # XXXX - for now, if it's a duplicate name, just punt and avoid
    # producing the error
    if [ ${resultname%%.*} == "DUPLICATE__NAME" ] ;then
        ndups=$ndups+1
        # FIXME: quarantine $result
        continue
    fi

    basedir=$(dirname $link)
    hostname=$(basename $basedir)

    # make sure that all the relevant state directories exist
    mk_dirs $hostname
    # ... and a couple of other necessities.
    if [[ $? -ne 0 ]] ;then
        echo "$TS: Creation of $hostname processing directories failed for $result: code $status" | tee -a $mail_content >&4
        nerrs=$nerrs+1
        # FIXME: quarantine $result
        continue
    fi
    mkdir -p $INCOMING/$hostname
    if [[ $? -ne 0 ]] ;then
        echo "$TS: Creation of $INCOMING/$hostname failed for $result: code $status" | tee -a $mail_content >&4
        nerrs=$nerrs+1
        # FIXME: quarantine $result
        continue
    fi
    incoming=$INCOMING/$hostname/$resultname
    if [ -e $incoming ]; then
        echo "$TS: Incoming result, $INCOMING/$hostname/$resultname/, already exists, skipping $result" | tee -a $mail_content >&4
        nerrs=$nerrs+1
        # FIXME: quarantine $result
        continue
    fi

    mkdir -p $INCOMING/$hostname
    status=$?
    if [[ $status -ne 0 ]] ;then
        echo "$TS: mkdir -p $INCOMING/$hostname failed for $result: code $status" | tee -a $mail_content >&4
        nerrs=$nerrs+1
        # FIXME: quarantine $result
        continue
    fi
    if [ -e $INCOMING/$hostname/${resultname} ]; then
        echo "$TS: $INCOMING/$hostname/${resultname} already exists for $result" | tee -a $mail_content >&4
        nerrs=$nerrs+1
        # FIXME: quarantine $result
        continue
    fi

    mkdir $INCOMING/$hostname/${resultname}.unpack
    status=$?
    if [[ $status -ne 0 ]] ;then
        echo "$TS: 'mkdir ${resultname}.unpack' failed for $result: code $status" | tee -a $mail_content >&4
        nerrs=$nerrs+1
        # FIXME: quarantine $result
        popd > /dev/null 2>&1
        continue
    fi
    let start_time=$(timestamp-seconds-since-epoch)
    tar --extract --no-same-owner --touch --delay-directory-restore --file="$result" --force-local --directory="$INCOMING/$hostname/${resultname}.unpack"
    status=$?
    if [[ $status -ne 0 ]] ;then
        echo "$TS: 'tar -xf $result' failed: code $status" | tee -a $mail_content >&4
        nerrs=$nerrs+1
        # FIXME: quarantine $result
        continue
    fi
    mv $INCOMING/$hostname/${resultname}.unpack/${resultname} $INCOMING/$hostname/${resultname}
    status=$?
    if [[ $status -ne 0 ]] ;then
        echo "$TS: '$result' does not contain ${resultname} directory at the top level; skipping" | tee -a $mail_content >&4
        rm -rf $INCOMING/$hostname/${resultname}.unpack
        nerrs=$nerrs+1
        # FIXME: quarantine $result
        continue
    fi
    rmdir $INCOMING/$hostname/${resultname}.unpack
    status=$?
    if [[ $status -ne 0 ]] ;then
        echo "$TS: WARNING - '$result' should only contain the ${resultname} directory at the top level, ignoring other content" | tee -a $mail_content >&4
        rm -rf $INCOMING/$hostname/${resultname}.unpack
        nwarn=$nwarn+1
    fi

    # chmod directories to at least 555
    find $INCOMING/$hostname/${resultname} -type d -print0 | xargs -0 chmod ugo+rx
    status=$?
    if [[ $status -ne 0 ]] ;then
        echo "$TS: 'chmod ugo+rx' of subdirs $resultname for $result failed: code $status" | tee -a $mail_content >&4
        nerrs=$nerrs+1
        rm -rf $INCOMING/$hostname/$resultname
        # FIXME: quarantine $result
        continue
    fi

    # chmod files to at least 444
    chmod -R ugo+r $INCOMING/$hostname/$resultname
    status=$?
    if [[ $status -ne 0 ]] ;then
        echo "$TS: 'chmod -R ugo+r $resultname' for $result failed: code $status" | tee -a $mail_content >&4
        nerrs=$nerrs+1
        rm -rf $INCOMING/$hostname/$resultname
        # FIXME: quarantine $result
        continue
    fi

    # Version 002 agents use the metadata log to store a prefix.
    # They may also store a user option in the metadata log.
    # We check for both of these here (n.b. if nothing is found
    # they are going to be empty strings):
    prefix=$(getconf.py -C $INCOMING/$hostname/$resultname/metadata.log prefix run)
    user=$(getconf.py -C $INCOMING/$hostname/$resultname/metadata.log user run)

    # Version 001 agents use a prefix file.  If there is a prefix file,
    # create a link as specified in the prefix file.  pbench-dispatch
    # has already moved it to the .prefix subdir
    prefixfile=$basedir/.prefix/$resultname.prefix
    if [ -f $prefixfile ] ;then
        # add the slash to make both cases uniform in what follows
        prefix=$(cat $prefixfile)
    fi

    # if non-empty and does not contain a trailing slash, add one
    if [ ! -z $prefix -a ${prefix%/} = ${prefix} ] ;then
        prefix=${prefix}/
    fi

    mkdir -p $RESULTS/$hostname/$prefix
    status=$?
    if [[ $status -ne 0 ]] ;then
        echo "$TS: mkdir -p $RESULTS/$hostname/$prefix for $result failed: code $status" | tee -a $mail_content >&4
        nerrs=$nerrs+1
        rm -rf $INCOMING/$hostname/$resultname
        continue
    else
        # make a link in results/
        echo "ln -s $incoming $RESULTS/$hostname/$prefix$resultname"
        ln -s $incoming $RESULTS/$hostname/$prefix$resultname
        status=$?
        if [[ $status -ne 0 ]] ;then
            echo "$TS: ln -s $incoming $RESULTS/$hostname/$prefix$resultname for $result failed: code $status" | tee -a $mail_content >&4
            nerrs=$nerrs+1
            rm -rf $incoming
            rm -rf $INCOMING/$hostname/$resultname
            continue
        fi

        if [ ! -z ${user} ] ;then
            # make a link in users/ but first make sure the directory exists
            mkdir -p ${USERS}/${user}/${hostname}/${prefix}
            echo "ln -s ${incoming} $USERS/${user}/${hostname}/${prefix}${resultname}"
            ln -s ${incoming} $USERS/${user}/${hostname}/${prefix}${resultname}
            status=$?
            if [[ $status -ne 0 ]] ;then
                echo "$TS: code $status: ln -s ${incoming} $USERS/${user}/${hostname}/${prefix}${resultname}" | tee -a $mail_content >&4
                nerrs=$nerrs+1
                rm -rf ${incoming}
                rm -rf ${INCOMING}/${hostname}/${resultname}
                rm -rf ${RESULTS}/${hostname}/${prefix}${resultname}
                continue
            fi
        fi
    fi

    mv $ARCHIVE/$hostname/$linksrc/$resultname.tar.xz $ARCHIVE/$hostname/$linkdest/$resultname.tar.xz
    status=$?
    if [[ $status -ne 0 ]] ;then
        echo "$TS: Cannot move symlink to $ARCHIVE/$hostname/$resultname.tar.xz from $linksrc to $linkdest: code $status" | tee -a $mail_content >&4
        nerrs=$nerrs+1
        # Cleanup needed here but trap takes care of it.
        rm -rf ${incoming}
        rm -rf ${INCOMING}/${hostname}/${resultname}
        rm $RESULTS/$hostname/$prefix$resultname
        continue
    fi

    # Create a link in each state dir - if any fail, we should delete them all? No, that
    # would be racy.
    let toterr=0
    let totsuc=0
    for state in ${linkdestlist}; do
        ln -sf ${ARCHIVE}/${hostname}/${resultname}.tar.xz ${ARCHIVE}/${hostname}/${state}/${resultname}.tar.xz
        status=$?
        if [ ${status} -eq 0 ] ;then
            let totsuc+=1
        else
            echo "$TS: Cannot create ${ARCHIVE}/${hostname}/${resultname}.tar.xz link to $state: code $status" | tee -a $mail_content >&4
            let toterr+=1
        fi
    done
    if [ $toterr -gt 0 ]; then
        # Count N link creations as one error since it is for handling of a
        # single tarball.
        let nerrs+=1
    fi

    let end_time=$(timestamp-seconds-since-epoch)
    let duration=end_time-start_time
    # log the success
    echo "$TS: $hostname/$resultname: success - elapsed time (secs): $duration - size (bytes): $size"
    ntb=$ntb+1
done < $list

echo "$TS: Processed $ntb tarballs"

log_finish

subj="$PROG.$TS($PBENCH_ENV) - w/ $nerrs errors"
cat << EOF > $index_content
$subj
Processed $ntotal result tar balls, $ntb successfully, $nwarn warnings, $nerrs errors, and $ndups duplicates

EOF
cat $mail_content >> $index_content
pbench-report-status --name $PROG --timestamp $(timestamp) --type status $index_content

exit 0
