#!/usr/bin/python3
# Export and import blocklists via API

import argparse
import toml
import csv
import requests
import json
import time
import os.path
import urllib.request as urlr

import logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')

# Max size of a URL-fetched blocklist
URL_BLOCKLIST_MAXSIZE = 1024 ** 3

log = logging.getLogger('fediblock_sync')

CONFIGFILE = "/home/mastodon/etc/admin.conf"

# The relative severity levels of blocks
SEVERITY = {
    'noop': 0,
    'silence': 1,
    'suspend': 2,
}

# Default for 'reject_media' setting for each severity level
REJECT_MEDIA_DEFAULT = {
    'noop': False,
    'silence': True,
    'suspend': True,
}

# Default for 'reject_reports' setting for each severity level
REJECT_REPORTS_DEFAULT = {
    'noop': False,
    'silence': True,
    'suspend': True,
}

def sync_blocklists(conf: dict):
    """Sync instance blocklists from remote sources.

    @param conf: A configuration dictionary
    """
    # Build a dict of blocklists we retrieve from remote sources.
    # We will merge these later using a merge algorithm we choose.

    blocklists = {}
    # Fetch blocklists from URLs
    if not conf.no_fetch_url:
        log.info("Fetching domain blocks from URLs...")
        for listurl in conf.blocklist_url_sources:
            blocklists[listurl] = []
            with urlr.urlopen(listurl) as fp:
                rawdata = fp.read(URL_BLOCKLIST_MAXSIZE).decode('utf-8')
                reader = csv.DictReader(rawdata.split('\n'))
                for row in reader:
                    # Coerce booleans from string to Python bool
                    for boolkey in ['reject_media', 'reject_reports', 'obfuscate']:
                        if boolkey in row:
                            row[boolkey] = str2bool(row[boolkey])
                    blocklists[listurl].append(row)

            if conf.save_intermediate:
                save_intermediate_blocklist(blocklists[listurl], listurl, conf.savedir)

    # Fetch blocklists from remote instances
    if not conf.no_fetch_instance:
        log.info("Fetching domain blocks from instances...")
        for blocklist_src in conf.blocklist_instance_sources:
            domain = blocklist_src['domain']
            token = blocklist_src['token']
            blocklists[domain] = fetch_instance_blocklist(token, domain)
            if conf.save_intermediate:
                save_intermediate_blocklist(blocklists[domain], domain, conf.savedir, conf.include_private_comments)

    # Merge blocklists into an update dict
    merged = merge_blocklists(blocklists, conf.mergeplan, conf.include_private_comments)
    if conf.blocklist_savefile:
        log.info(f"Saving merged blocklist to {conf.blocklist_savefile}")
        save_blocklist_to_file(merged.values(), conf.blocklist_savefile, conf.include_private_comments)

    # Push the blocklist to destination instances
    if not conf.no_push_instance:
        log.info("Pushing domain blocks to instances...")
        for dest in conf.blocklist_instance_destinations:
            domain = dest['domain']
            token = dest['token']
            push_blocklist(token, domain, merged.values(), conf.dryrun, conf.include_private_comments)

def merge_blocklists(blocklists: dict, mergeplan: str='max',
                    include_private_comments: bool=False) -> dict:
    """Merge fetched remote blocklists into a bulk update

    @param mergeplan: An optional method of merging overlapping block definitions
        'max' (the default) uses the highest severity block found
        'min' uses the lowest severity block found
    @param include_private_comments: Include private comments in merged blocklist. Defaults to False.
    """
    merged = {}

    for key, blist in blocklists.items():
        log.debug(f"processing key {key} blist...")
        for newblock in blist:
            domain = newblock['domain']
            if domain in merged:
                log.debug(f"Overlapping block for domain {domain}. Merging...")
                blockdata = apply_mergeplan(merged[domain], newblock, mergeplan, include_private_comments)
            else:
                # New block
                blockdata = {
                    'domain': newblock['domain'],
                    # Default to Silence if nothing is specified
                    'severity': newblock.get('severity', 'silence'),
                    'public_comment': newblock.get('public_comment', ''),
                    'obfuscate': newblock.get('obfuscate', True), # default obfuscate to True
                }
                sev = blockdata['severity'] # convenience variable
                blockdata['reject_media'] = newblock.get('reject_media', REJECT_MEDIA_DEFAULT[sev])
                blockdata['reject_reports'] = newblock.get('reject_reports', REJECT_REPORTS_DEFAULT[sev])
                if include_private_comments:
                    blockdata['private_comment']: newblock.get('private_comment', '')

            # end if
            log.debug(f"blockdata is: {blockdata}")
            merged[domain] = blockdata
        # end for
    return merged

def apply_mergeplan(oldblock: dict, newblock: dict,
                    mergeplan: str='max',
                    include_private_comments: bool=False) -> dict:
    """Use a mergeplan to decide how to merge two overlapping block definitions
    
    @param oldblock: The exist block definition.
    @param newblock: The new block definition we want to merge in.
    @param mergeplan: How to merge. Choices are 'max', the default, and 'min'.
    @param include_private_comments: Include private comments in merged blocklist. Defaults to False.
    """
    # Default to the existing block definition
    blockdata = oldblock.copy()

    # If the public or private comment is different,
    # append it to the existing comment, joined with a newline
    # unless the comment is None or an empty string
    keylist = ['public_comment']
    if include_private_comments:
        keylist.append('private_comment')
    for key in keylist:
        try:
            if oldblock[key] != newblock[key] and newblock[key] not in ['', None]:
                blockdata[key] = '\n'.join([oldblock[key], newblock[key]])
        except KeyError:
            log.debug(f"Key '{key}' missing from block definition so cannot compare. Continuing...")
            continue
    
    # How do we override an earlier block definition?
    if mergeplan in ['max', None]:
        # Use the highest block level found (the default)
        log.debug(f"Using 'max' mergeplan.")

        if SEVERITY[newblock['severity']] > SEVERITY[oldblock['severity']]:
            log.debug(f"New block severity is higher. Using that.")
            blockdata['severity'] = newblock['severity']
        
        # If obfuscate is set and is True for the domain in
        # any blocklist then obfuscate is set to false.
        if newblock.get('obfuscate', False):
            blockdata['obfuscate'] = True

    elif mergeplan in ['min']:
        # Use the lowest block level found
        log.debug(f"Using 'min' mergeplan.")

        if SEVERITY[newblock['severity']] < SEVERITY[oldblock['severity']]:
            blockdata['severity'] = newblock['severity']

        # If obfuscate is set and is False for the domain in
        # any blocklist then obfuscate is set to False.
        if not newblock.get('obfuscate', True):
            blockdata['obfuscate'] = False

    else:
        raise NotImplementedError(f"Mergeplan '{mergeplan}' not implemented.")

    log.debug(f"Block severity set to {blockdata['severity']}")
    # Use the severity level to set rejections, if not defined in newblock
    # If severity level is 'suspend', it doesn't matter what the settings is for
    # 'reject_media' or 'reject_reports'
    blockdata['reject_media'] = newblock.get('reject_media', REJECT_MEDIA_DEFAULT[blockdata['severity']])
    blockdata['reject_reports'] = newblock.get('reject_reports', REJECT_REPORTS_DEFAULT[blockdata['severity']])
    
    log.debug(f"set reject_media to: {blockdata['reject_media']}")
    log.debug(f"set reject_reports to: {blockdata['reject_reports']}")

    return blockdata

def fetch_instance_blocklist(token: str, host: str) -> list:
    """Fetch existing block list from server

    @param token: The OAuth Bearer token to authenticate with.
    @param host: The remote host to connect to.
    @returns: A list of the admin domain blocks from the instance.
    """
    log.info(f"Fetching instance blocklist from {host} ...")
    api_path = "/api/v1/admin/domain_blocks"

    url = f"https://{host}{api_path}"

    domain_blocks = []
    link = True

    while link:
        response = requests.get(url, headers={'Authorization': f"Bearer {token}"})
        if response.status_code != 200:
            log.error(f"Cannot fetch remote blocklist: {response.content}")
            raise ValueError("Unable to fetch domain block list: %s", response)
        domain_blocks.extend(json.loads(response.content))
        
        # Parse the link header to find the next url to fetch
        # This is a weird and janky way of doing pagination but
        # hey nothing we can do about it we just have to deal
        link = response.headers['Link']
        pagination = link.split(', ')
        if len(pagination) != 2:
            link = None
            break
        else:
            next = pagination[0]
            prev = pagination[1]
        
            urlstring, rel = next.split('; ')
            url = urlstring.strip('<').rstrip('>')

    log.debug(f"Found {len(domain_blocks)} existing domain blocks.")
    return domain_blocks

def delete_block(token: str, host: str, id: int):
    """Remove a domain block"""
    log.debug(f"Removing domain block {id} at {host}...")
    api_path = "/api/v1/admin/domain_blocks/"

    url = f"https://{host}{api_path}{id}"

    response = requests.delete(url,
        headers={'Authorization': f"Bearer {token}"}
    )
    if response.status_code != 200:
        if response.status_code == 404:
            log.warn(f"No such domain block: {id}")
            return

        raise ValueError(f"Something went wrong: {response.status_code}: {response.content}")

def update_known_block(token: str, host: str, blockdict: dict):
    """Update an existing domain block with information in blockdict"""
    api_path = "/api/v1/admin/domain_blocks/"

    id = blockdict['id']
    blockdata = blockdict.copy()
    del blockdata['id']

    url = f"https://{host}{api_path}{id}"

    response = requests.put(url,
        headers={'Authorization': f"Bearer {token}"},
        data=blockdata
    )
    if response.status_code != 200:
        raise ValueError(f"Something went wrong: {response.status_code}: {response.content}")

def add_block(token: str, host: str, blockdata: dict):
    """Block a domain on Mastodon host
    """
    log.debug(f"Blocking domain {blockdata['domain']} at {host}...")
    api_path = "/api/v1/admin/domain_blocks"

    url = f"https://{host}{api_path}"

    response = requests.post(url,
        headers={'Authorization': f"Bearer {token}"},
        data=blockdata
    )
    if response.status_code != 200:
        raise ValueError(f"Something went wrong: {response.status_code}: {response.content}")

def push_blocklist(token: str, host: str, blocklist: list[dict],
                    dryrun: bool=False,
                    include_private_comments: bool=False):
    """Push a blocklist to a remote instance.
    
    Merging the blocklist with the existing list the instance has,
    updating existing entries if they exist.

    @param token: The Bearer token for OAUTH API authentication
    @param host: The instance host, FQDN or IP
    @param blocklist: A list of block definitions. They must include the domain.
    @param include_private_comments: Include private comments in merged blocklist. Defaults to False.
    """
    log.info(f"Pushing blocklist to host {host} ...")
    # Fetch the existing blocklist from the instance
    serverblocks = fetch_instance_blocklist(token, host)

    # Convert serverblocks to a dictionary keyed by domain name
    knownblocks = {row['domain']: row for row in serverblocks}

    for newblock in blocklist:

        log.debug(f"applying newblock: {newblock}")
        try:
            oldblock = knownblocks[newblock['domain']]
            log.debug(f"Block already exists for {newblock['domain']}, merging data...")

            # Check if anything is actually different and needs updating
            change_needed = False
            keylist = [
                'severity',
                'public_comment',
                'reject_media',
                'reject_reports',
                'obfuscate',
            ]
            if include_private_comments:
                keylist.append('private_comment')

            for key in keylist:
                try:
                    log.debug(f"Compare {key} '{oldblock[key]}' <> '{newblock[key]}'")
                    oldval = oldblock[key]
                    newval = newblock[key]
                    if oldval != newval:
                        log.debug("Difference detected. Change needed.")
                        change_needed = True
                        break

                except KeyError:
                    log.debug(f"Key '{key}' missing from block definition so cannot compare. Continuing...")
                    continue
            
            if change_needed:
                log.info(f"Change detected. Updating domain block for {oldblock['domain']}")
                blockdata = oldblock.copy()
                blockdata.update(newblock)
                if not dryrun:
                    update_known_block(token, host, blockdata)
                    # add a pause here so we don't melt the instance
                    time.sleep(1)
                else:
                    log.info("Dry run selected. Not applying changes.")

            else:
                log.debug("No differences detected. Not updating.")
                pass

        except KeyError:
            # This is a new block for the target instance, so we
            # need to add a block rather than update an existing one
            blockdata = {
                'domain': newblock['domain'],
                # Default to Silence if nothing is specified
                'severity': newblock.get('severity', 'silence'),
                'public_comment': newblock.get('public_comment', ''),
                'private_comment': newblock.get('private_comment', ''),
                'reject_media': newblock.get('reject_media', False),
                'reject_reports': newblock.get('reject_reports', False),
                'obfuscate': newblock.get('obfuscate', False),
            }
            log.info(f"Adding new block for {blockdata['domain']}...")
            if not dryrun:
                add_block(token, host, blockdata)
                # add a pause here so we don't melt the instance
                time.sleep(1)
            else:
                log.info("Dry run selected. Not adding block.")

def load_config(configfile: str):
    """Augment commandline arguments with config file parameters
    
    Config file is expected to be in TOML format
    """
    conf = toml.load(configfile)
    return conf

def save_intermediate_blocklist(
    blocklist: list[dict], source: str,
    filedir: str,
    include_private_comments: bool=False):
    """Save a local copy of a blocklist we've downloaded
    """
    # Invent a filename based on the remote source
    # If the source was a URL, convert it to something less messy
    # If the source was a remote domain, just use the name of the domain
    log.debug(f"Saving intermediate blocklist from {source}")
    source = source.replace('/','-')
    filename = f"{source}.csv"
    filepath = os.path.join(filedir, filename)
    save_blocklist_to_file(blocklist, filepath, include_private_comments)

def save_blocklist_to_file(
    blocklist: list[dict],
    filepath: str,
    include_private_comments: bool=False):
    """Save a blocklist we've downloaded from a remote source

    @param blocklist: A dictionary of block definitions, keyed by domain
    @param filepath: The path to the file the list should be saved in.
    @param include_private_comments: Include private comments in merged blocklist. Defaults to False.
    """
    try:
        blocklist = sorted(blocklist, key=lambda x: x['domain'])
    except KeyError:
        log.error("Field 'domain' not found in blocklist. Are you sure the URLs are correct?")
        log.debug(f"blocklist is: {blocklist}")

    if include_private_comments:
        fieldnames = ['domain', 'severity', 'private_comment', 'public_comment', 'reject_media', 'reject_reports', 'obfuscate']
    else:
        fieldnames = ['domain', 'severity', 'public_comment', 'reject_media', 'reject_reports', 'obfuscate']
    with open(filepath, "w") as fp:
        writer = csv.DictWriter(fp, fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(blocklist)

def augment_args(args):
    """Augment commandline arguments with config file parameters"""
    conf = toml.load(args.config)

    if not args.no_fetch_url:
        args.no_fetch_url = conf.get('no_fetch_url', False)

    if not args.no_fetch_instance:
        args.no_fetch_instance = conf.get('no_fetch_instance', False)

    if not args.no_push_instance:
        args.no_push_instance = conf.get('no_push_instance', False)

    if not args.blocklist_savefile:
        args.blocklist_savefile = conf.get('blocklist_savefile', None)

    if not args.save_intermediate:
        args.save_intermediate = conf.get('save_intermediate', False)
    
    if not args.savedir:
        args.savedir = conf.get('savedir', '/tmp')

    if not args.include_private_comments:
        args.include_private_comments = conf.get('include_private_comments', False)

    args.blocklist_url_sources = conf.get('blocklist_url_sources')
    args.blocklist_instance_sources = conf.get('blocklist_instance_sources')
    args.blocklist_instance_destinations = conf.get('blocklist_instance_destinations')

    return args

def str2bool(boolstring: str) -> bool:
    """Helper function to convert boolean strings to actual Python bools
    """
    boolstring = boolstring.lower()
    if boolstring in ['true', 't', '1', 'y', 'yes']:
        return True
    elif boolstring in ['false', 'f', '0', 'n', 'no']:
        return False
    else:
        raise ValueError(f"Cannot parse value '{boolstring}' as boolean")

if __name__ == '__main__':

    ap = argparse.ArgumentParser(description="Bulk blocklist tool",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument('-c', '--config', default='/etc/default/fediblockhole.conf.toml', help="Config file")

    ap.add_argument('-o', '--outfile', dest="blocklist_savefile", help="Save merged blocklist to a local file.")
    ap.add_argument('-S', '--save-intermediate', dest="save_intermediate", action='store_true', help="Save intermediate blocklists we fetch to local files.")
    ap.add_argument('-D', '--savedir', dest="savedir", help="Directory path to save intermediate lists.")
    ap.add_argument('-m', '--mergeplan', choices=['min', 'max'], default='max', help="Set mergeplan.")

    ap.add_argument('--no-fetch-url', dest='no_fetch_url', action='store_true', help="Don't fetch from URLs, even if configured.")
    ap.add_argument('--no-fetch-instance', dest='no_fetch_instance', action='store_true', help="Don't fetch from instances, even if configured.")
    ap.add_argument('--no-push-instance', dest='no_push_instance', action='store_true', help="Don't push to instances, even if configured.")
    ap.add_argument('--include-private-comments', dest='include_private_comments', action='store_true', help="Include private_comment field in exports and imports.")

    ap.add_argument('--loglevel', choices=['debug', 'info', 'warning', 'error', 'critical'], help="Set log output level.")
    ap.add_argument('--dryrun', action='store_true', help="Don't actually push updates, just show what would happen.")

    args = ap.parse_args()
    if args.loglevel is not None:
        levelname = args.loglevel.upper()
        log.setLevel(getattr(logging, levelname))

    # Load the configuration file
    args = augment_args(args)

    # Do the work of syncing
    sync_blocklists(args)