#!/usr/bin/env python3
'''
pulledpork3 v(whatever it says below!)

Copyright (C) 2021 Noah Dietrich, Colin Grady, Michael Shirk and the PulledPork Team!

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
'''

from argparse import ArgumentParser         # command line parameters parser
from json import load                       # to load json manifest file in lightSPD
from glob import glob                       # simple file path patern matching
from os import chmod, environ, listdir, kill
from os.path import isfile, join, sep, abspath
from platform import platform, version, uname, system, python_version, architecture
from re import search, sub, match, MULTILINE
from shutil import copy                     # remove directory tree, python 3.4+
try:
    from signal import SIGHUP               # linux/bsd, not windows
except ImportError:
    # from ctypes import CDLL, c_raise,      # Windows reload process (not yet implemented)
    pass
from subprocess import Popen, PIPE          # to get Snort version from binary

# Our PulledPork3 internal libraries
from lib import config, helpers, logger
from lib.snort import (Blocklist, Rules, Policies,
                       RulesArchive, RulesetTypes)


# -----------------------------------------------------------------------------
#   GLOBAL CONSTANTS
# -----------------------------------------------------------------------------

# Version is based on the following:
# First number will always be 3 (until Snort 4 or hell freezes over)
# Second number will be the major number (3.1.0.0 will be for a major updates only)
# Third number will be the minor number (3.0.1.0 for a number of bug fixes)
# Fourth number will be for any revisions between releases (to track builds)

__version__ = '3.0.0.5'

SCRIPT_NAME = 'PulledPork'
TAGLINE = 'Lowcountry yellow mustard bbq sauce is the best bbq sauce. Fight me.'
VERSION_STR = f'{SCRIPT_NAME} v{__version__}'

# URLs for supported rulesets (replace <version> and <oinkcode> when downloading)
RULESET_URL_SNORT_COMMUNITY = 'https://snort.org/downloads/community/snort3-community-rules.tar.gz'
RULESET_URL_SNORT_REGISTERED = 'https://snort.org/rules/snortrules-snapshot-<VERSION>.tar.gz'
RULESET_URL_SNORT_LIGHTSPD = 'https://snort.org/rules/Talos_LightSPD.tar.gz'

# TODO: Support for the ET Rulesets has not yet been implemented
# RULESET_URL_ET_OPEN = 'https://rules.emergingthreats.net/open/snort-<VERSION>/emerging.rules.tar.gz'
# RULESET_URL_ET_PRO = 'https://rules.emergingthreatspro.com/<ET_OINKCODE>/snort-<VERSION>/etpro.rules.tar.gz'

# URLs for supported blocklists
SNORT_BLOCKLIST_URL = 'https://snort.org/downloads/ip-block-list'
ET_BLOCKLIST_URL = 'http://rules.emergingthreatspro.com/fwrules/emerging-Block-IPs.txt'


# -----------------------------------------------------------------------------
#   Prepare the logging and config
# -----------------------------------------------------------------------------

log = logger.Logger()
conf = config.Config()


# -----------------------------------------------------------------------------
#   MAIN FUNCTION - program execution starts here.
# -----------------------------------------------------------------------------

def main():

    # parse our command-line args with ArgParse
    conf.args = parse_argv()

    # Setup logging as requested
    #   NOTE: For now all the args are permitted, but specifying more than one
    #         will override less verbose ones. Priority order:
    #               DEFAULT (info) < quiet < verbose < debug
    if conf.args.quiet:
        log.level = logger.Levels.WARNING
    if conf.args.verbose:
        log.level = logger.Levels.VERBOSE
    if conf.args.debug:
        log.level = logger.Levels.DEBUG

    # if the -V flag (version) was passed: Print the script Version and Exit
    if conf.args.version:
        print(VERSION_STR)
        flying_pig_banner()
        return

    # Always show pigs flying as the preamble, unless running in quiet mode
    if not conf.args.quiet:
        flying_pig_banner()

    # Print the env (will only print if verbose or debug)
    print_environment(conf)

    # Also setup halt on warn as requested
    log.halt_on_warn = not conf.args.ignore_warn

    # Save from args
    conf.delete_temp_path = not conf.args.keep_temp_dir

    # Load the configuration File from command line (-c FILENAME). Verify exists, and only 1 entry.
    if not conf.args.configuration:
        log.error("The following arguments are required: -c/--configuration <file>")
    if len(conf.args.configuration) > 1:
        log.warning('Multiple entries passed as -c/--configuration.  Only a single entry permitted.')

    config_file = conf.args.configuration[0]  # this is a list of one element

    # load configuration file
    log.info(f'Loading configuration file:  {config_file}')
    try:
        conf.load(config_file)
    except Exception as e:
        log.error(f'Unable to load configuration file:  {e}')

    # Before we log the config, add hidden string for oinkcode
    if conf.oinkcode and not conf.args.print_oinkcode:
        log.add_hidden_string(conf.oinkcode)

    # Print the read config before validation
    conf.log_config()

    # Attempt to validate the config
    conf.validate()

    target_dir = f'{SCRIPT_NAME}-{conf.start_time}'
    working_dir = helpers.WorkingDirectory(conf.temp_path, target_dir, conf.delete_temp_path)
    log.verbose(f'Working directory is:  {working_dir}')

    # Are we missing the Snort version in config?
    if not conf.defined('snort_version'):
        conf.snort_version = get_snort_version(conf.get('snort_path'))

    # we now have all required info to run, print the configuration to screen
    print_operational_settings()

    # -----------------------------------------------------------------------------
    # LOAD RULESETS
    # Obtain the archived ruleset (tgz) files
    # either from online sources or from a local folder

    log.debug('---------------------------------')
    log.verbose('Loading rulesets')

    # The RulesArchive objects used for loading
    loaded_rulesets = []

    # Helper function for loading rulesets

    def load_ruleset(filename=None, url=None, oinkcode=None):
        '''
        Load the specified ruleset, locally or from URL, and add to the rulesets list
        '''

        log.verbose(f'Loading rules archive:\n - Source:  {filename or url}')

        # Attempt to load the file and get the type
        try:
            rules_archive = RulesArchive(filename=filename, url=url, oinkcode=oinkcode)
            ruleset_type = rules_archive.ruleset
        except Exception as e:
            log.warning(f'Unable to load rules archive:  {e}')
            return
        log.verbose(f' - Loaded as:  {ruleset_type.value}')

        # Save the ruleset
        try:
            written_file = rules_archive.write_file(working_dir.downloaded_path)
        except Exception as e:
            log.warning(f'Unable to save rules archive:  {e}')
            return
        log.verbose(f' - Saved as:  {written_file}')

        # Appends the loaded ruleset
        loaded_rulesets.append(rules_archive)

    # End helper

    # Loading from a local file?
    if conf.args.file:
        log.debug(f'Using one file for ruleset source (not downloading rulesets):\n - {conf.args.file}')
        load_ruleset(filename=conf.args.file)

    # Loading from a local folder?
    elif conf.args.folder:
        log.debug(f'Using all files for ruleset source (not downloading) from:\n - {conf.args.folder}')
        for path in listdir(conf.args.folder):
            full_path = join(conf.args.folder, path)
            if isfile(full_path) and (full_path.endswith('tar.gz') or (full_path.endswith('tgz'))):
                load_ruleset(filename=full_path)

    # Loading from the Snort rulesets?
    else:
        log.debug('Downloading Snort rulesets from Internet')

        if conf.community_ruleset:
            load_ruleset(url=RULESET_URL_SNORT_COMMUNITY)

        if conf.registered_ruleset:
            version = sub(r'[^a-zA-Z0-9]', '', conf.snort_version)  # version in URL is alphanumeric only
            reg_url = RULESET_URL_SNORT_REGISTERED.replace('<VERSION>', version)
            load_ruleset(url=reg_url, oinkcode=conf.oinkcode)

        if conf.lightspd_ruleset:
            load_ruleset(url=RULESET_URL_SNORT_LIGHTSPD, oinkcode=conf.oinkcode)

    if not len(loaded_rulesets):
        log.error('No rulesets were loaded')

    # extract rulesets to folder (tupple with ID, full path of folders for extracted rulesets)
    extract_rulesets(loaded_rulesets, working_dir.extracted_path)

    # -----------------------------------------------------------------------------
    # PROCESS RULESETS HERE

    log.debug('---------------------------------')
    log.verbose('Processing rulesets')

    all_new_rules = Rules()
    all_new_policies = Policies()

    for loaded_ruleset in loaded_rulesets:

        # Save the extracted path to a shorter named var
        ruleset_path = loaded_ruleset.extracted_path

        # determine ruleset type:
        if loaded_ruleset.ruleset == RulesetTypes.COMMUNITY:

            log.info('Processing Community ruleset')
            log.verbose(f' - Ruleset path:  {ruleset_path}')

            # only simple rules to worry about
            # community rules have an extra folder to delve into
            rule_path = join(ruleset_path, 'snort3-community-rules')

            # todo: wrap next line in try/catch
            community_rules = Rules(rule_path, conf.ignored_files)

            # Generate the community policy from the rules
            # commmunity rules don't come with a policy file, so create one (in case the rule_mode = policy)
            community_policy = community_rules.policy_from_state(conf.ips_policy)

            log.verbose('Finished processing Community ruleset')
            log.verbose(f' - Community Rules:  {community_rules}')
            log.verbose(f' - Community Policy:  {community_policy}')

            all_new_rules.extend(community_rules)
            all_new_policies.extend(community_policy)

        elif loaded_ruleset.ruleset == RulesetTypes.REGISTERED:

            log.info('Processing Registered ruleset')
            log.verbose(f' - Ruleset path:  {ruleset_path}')

            # process text rules
            text_rules_path = join(ruleset_path, 'rules')
            registered_rules = Rules(text_rules_path, conf.ignored_files)
            registered_policies = Policies(text_rules_path)

            log.debug(f' - Text Rules:  {registered_rules}')
            log.debug(f' - Text Policies:  {registered_policies}')

            # process builtin rules
            builtin_rules_path = join(ruleset_path, 'builtins')
            builtin_rules = Rules(builtin_rules_path)
            builtin_policies = Policies(builtin_rules_path)

            log.debug(f' - Builtin Rules:  {builtin_rules}')
            log.debug(f' - Builtin Policies:  {builtin_policies}')

            registered_rules.extend(builtin_rules)
            registered_policies.extend(builtin_policies)

            # process so rules
            if conf.defined('sorule_path'):
                # copy files first to temp\so_rules folder (we'll copy them all at the end, this checks for dupes)
                # todo: error handling
                so_src_folder = join(ruleset_path, 'so_rules', 'precompiled', conf.distro)
                src_files = listdir(so_src_folder)
                for file_name in src_files:
                    full_file_name = join(so_src_folder, file_name)
                    if isfile(full_file_name):
                        copy(full_file_name, working_dir.so_rules_path)

                # get SO rule stubs
                # todo: generate stubs if distro folder doesn't exist
                so_rules_path = join(ruleset_path, 'so_rules')

                so_rules = Rules(so_rules_path)
                so_policies = Policies(so_rules_path)

                log.debug(f' - SO Rules:  {so_rules}')
                log.debug(f' - SO Policies:  {so_policies}')

                registered_rules.extend(so_rules)
                registered_policies.extend(so_policies)

            log.verbose(f'Preparing to apply policy {conf.ips_policy} to Registered rules')
            log.debug(f' - Registered rules before policy application:  {registered_rules}')

            # apply the policy to these rules
            registered_rules.apply_policy(registered_policies[conf.ips_policy])

            log.verbose('Finished processing Registered ruleset')
            log.verbose(f' - Registered Rules:  {registered_rules}')
            log.verbose(f' - Registered Policies:  {registered_policies}')

            all_new_rules.extend(registered_rules)
            all_new_policies.extend(registered_policies)

        elif loaded_ruleset.ruleset == RulesetTypes.LIGHTSPD:

            log.info('Processing LightSPD ruleset')
            log.verbose(f' - Ruleset path:  {ruleset_path}')

            lightspd_rules = Rules()
            lightspd_policies = Policies()

            # load .so rules IFF sorule_path is configured.
            # if 'distro' is not configured, then we need to compile the rules ourself
            # right now: we only use the manifest.json file for processing .so rules
            if conf.defined('sorule_path') and conf.defined('distro'):
                log.debug('Trying to load precompiled so rules')
                json_manifest_file = join(ruleset_path, 'lightspd', 'manifest.json')

                # load json manfiest file to identify .so rules location
                log.debug('Processing json manifest file ' + json_manifest_file)
                with open(json_manifest_file) as f:
                    manifest = load(f)

                manifest_versions = []
                for i in manifest["snort versions"]:
                    manifest_versions.append(i)

                log.debug('Found ' + str(len(manifest_versions)) + ' versions of snort in the manifest file:  ' + str(manifest_versions))

                # we need to normalize and then sort the version numbers.
                normalized_versions = {}
                for m in manifest_versions:
                    normalized_versions[normalize_version_number(m)] = m

                sorted_versions = list(normalized_versions.keys())
                sorted_versions.sort(key=lambda s: list(map(int, s.split('.'))), reverse=True)

                # find version number in the json file that is the largest number just below or equal to the version of snort3.
                log.debug(f'Looking for a version in the manifest file that is less than or equal to our current snort Version:  {conf.snort_version}')
                version_to_use = None
                normalized_snort_version = normalize_version_number(conf.snort_version)

                log.debug(f'after version normalization, snort version is {normalized_snort_version}.')
                log.debug(f'   normalized manifest file versions are: {normalized_versions}')
                log.debug(f'   normalized sorted list of versions are: {sorted_versions}')

                for v in sorted_versions:
                    # if v <= normalized_snort_version:
                    if version_equal_or_lesser(normalized_snort_version, v):
                        version_to_use = v
                        break

                if version_to_use is None:
                    log.warning("Not able to find a valid snort version in the lightSPD manifest file. not processing any SO rules from the lightSPD package.")
                else:
                    version_to_use = normalized_versions[version_to_use]
                    log.debug("Using snort version " + version_to_use + ' from lightSPD manifest file. Actual Snort version is:  ' + conf.snort_version)

                    # get other data from manifest file for the selected version
                    policies_path = manifest["snort versions"][version_to_use]['policies_path']
                    policies_path = policies_path.replace('/', sep)
                    log.debug('policies_path from lightSPD Manifest file for snort ' + version_to_use + ' is:  ' + policies_path)

                    # todo: try/catch next line in case the arch. doesn't exist
                    modules_path = manifest["snort versions"][version_to_use]['architectures'][conf.distro]["modules_path"]
                    modules_path = modules_path.replace('/', sep)
                    log.debug('modules_path from lightSPD Manifest file for snort ' + version_to_use + ' is:  ' + modules_path)

                    # copy so files from our archive to working folder
                    so_src_folder = join(ruleset_path, 'lightspd', modules_path, 'so_rules')
                    src_files = listdir(so_src_folder)
                    for file_name in src_files:
                        full_file_name = join(so_src_folder, file_name)
                        if isfile(full_file_name):
                            copy(full_file_name, working_dir.so_rules_path)

                    # get SO rule stub files
                    # todo: generate stubs if distro folder doesn't exist
                    so_rules_path = join(ruleset_path, 'lightspd', 'modules', 'stubs')
                    # r = get_text_rules_from_folder(so_rules_path, 'SNORT_LIGHTSPD', 'snort_ruleset', 'so')
                    # rules.extend(r)
                    lightspd_rules = Rules(so_rules_path)
                    lightspd_policies = Policies(so_rules_path)

                log.debug(f' - SO Rules processed:  {lightspd_rules}')
                log.debug(f' - SO Policies processed:  {lightspd_policies}')

            elif conf.defined('sorule_path'):
                log.debug('Trying to compile .so rules (no distro specified)')
                lightspd_rules, lightspd_policies = compile_so_rules(join(ruleset_path, 'lightspd', 'modules', 'src'), working_dir.so_rules_path)

            else:
                log.debug(f'No so rules to process.')

            # LOAD TEXT RULES FROM LightSPD archive
            # right now, the LightSPD archive only has a 3.0.0.0 folder in it, so let's use that explicitly.
            # this should hopefully be changed to an explicit entry in the manifest.json file
            text_rules_path = join(ruleset_path, 'lightspd', 'rules', '3.0.0.0')

            lightspd_text_rules = Rules(text_rules_path, conf.ignored_files)
            lightspd_text_policies = Policies(text_rules_path)

            log.debug(f' - text Rules processed:  {lightspd_text_rules}')
            log.debug(f' - text Policies processed:  {lightspd_text_policies}')

            lightspd_rules.extend(lightspd_text_rules)
            lightspd_policies.extend(lightspd_text_policies)

            # LOAD BULTIN RULES FROM LightSPD archive
            # right now, the LightSPD folder has a single 3.0.0.0-0 folder in it, so let's use that explictly
            # hopefully this will be changed to an explicit entry in the manifest.json file
            builtin_rules_path = join(ruleset_path, 'lightspd', 'builtins', '3.0.0.0-0')
            lightspd_builtin_rules = Rules(builtin_rules_path, conf.ignored_files)
            lightspd_builtin_policies = Policies(builtin_rules_path)

            log.debug(f' - builtin Rules processed:  {lightspd_builtin_rules}')
            log.debug(f' - builtin Policies processed:  {lightspd_builtin_policies}')

            lightspd_rules.extend(lightspd_builtin_rules)
            lightspd_policies.extend(lightspd_builtin_policies)

            log.verbose(f'Preparing to apply policy {conf.ips_policy} to LightSPD rules')
            log.debug(f' - LightSPD rules before policy application:  {lightspd_rules}')

            # apply the policy to these rules
            lightspd_rules.apply_policy(lightspd_policies[conf.ips_policy])

            log.verbose('Finished processing LightSPD ruleset')
            log.verbose(f' - LightSPD Rules:  {lightspd_rules}')
            log.verbose(f' - LightSPD Policies:  {lightspd_policies}')

            all_new_rules.extend(lightspd_rules)
            all_new_policies.extend(lightspd_policies)

        else:
            log.warning("Unknown ruleset archive folder recieved.")
            # TODO: non-standard ruleset, we need to figure it out

    if len(conf.local_rules):

        log.verbose('Completed processing all rulesets before local rulesets:')
        log.verbose(f' - Collected Rules:  {all_new_rules}')
        log.verbose(' - Collected Policies:')
        for policy in all_new_policies:
            log.verbose(f'    - {policy}')

        for path in conf.local_rules:
            local_rules = Rules(path)
            log.info(f'loaded local rules file:  {local_rules} from {path}')
            all_new_rules.extend(local_rules)

            # local rules don't come with a policy file, so create one (in case the rule_mode = policy)
            all_new_policies.extend(local_rules.policy_from_state(conf.ips_policy))

    log.info('Preparing to modify rules by sid file')
    # Modify Rules based on sid files
    for s in conf.state_order:
        log.debug(f'- checking to see if {s} sid file is set in conf:')
        if s == 'enable' and conf.defined('enablesid'):
            all_new_rules.load_sid_modification_file(conf.enablesid, 'enable')
        elif s == 'drop' and conf.defined('dropsid'):
            log.debug('dropsid is set in conf, will try to process.')
            all_new_rules.load_sid_modification_file(conf.dropsid, 'drop')
        elif s == 'disable' and conf.defined('disablesid'):
            all_new_rules.load_sid_modification_file(conf.disablesid, 'disable')
        else:
            # errorout todo
            pass

    log.info('Completed processing all rulesets and local rules:')
    log.info(f' - Collected Rules:  {all_new_rules}')
    log.info(' - Collected Policies:')
    for policy in all_new_policies:
        log.info(f'    - {policy}')

    # Prepare rules for output
    log.info(f'Writing rules to:  {conf.rule_path}')
    header = ('#-------------------------------------------------------------------\n'
              f'#  Rules file created by {SCRIPT_NAME}  at {conf.start_time}\n'
              '#  \n'
              '#  To Use this file: in your snort.lua, you need the following settings:\n'
              '#  ips =\n'
              '#  {{\n'
              f'#      include = "{conf.rule_path}",\n')
    if conf.rule_mode == 'policy':
        header += (f'#      states = "{conf.policy_path}",\n'
                   '#      ...\n'
                   '#  }}\n#\n'
                   '#  detection=\n'
                   '#  {{\n'
                   '#      global_default_rule_state = false,\n')
    header += '#      ...\n'
    header += '#  }}\n#\n'
    if conf.defined('sorule_path'):
        header += '# You have chosen to enable so rules.\n'
        header += '# To prevent errors when running snort, make sure to include\n'
        header += '# the following command-line option:\n'
        header += f'#    --plugin-path "{conf.sorule_path}"\n#\n'
    header += "#-------------------------------------------------------------------\n\n"

    # if rule_mode is policy, and disabled rules should be written, we need to
    # enable all rules (but not modify the policy) so that all disabled rules
    # are written without a hash mark.
    if conf.rule_mode == 'policy' and conf.include_disabled_rules:
        for rule in all_new_rules:
            rule.state = True

    # write rules to disk
    all_new_rules.write_file(conf.rule_path, conf.include_disabled_rules, header)

    # write the policy to disk
    if conf.rule_mode == 'policy':
        log.info(f'Writing policy file to:  {conf.policy_path}')
        (all_new_policies[conf.ips_policy]).write_file(conf.policy_path)

    # copy .so rules from tempdir
    # todo: delete old rules
    if conf.defined('sorule_path'):
        src_files = listdir(working_dir.so_rules_path)
        for file_name in src_files:
            full_file_name = join(working_dir.so_rules_path, file_name)
            if isfile(full_file_name):
                copy(full_file_name, conf.sorule_path)

    # -----------------------------------------------------------------------------
    # Download Blocklists

    # Have a blocklist out file defined AND have a blocklist to download?
    if conf.defined('blocklist_path') and any([conf.snort_blocklist, conf.et_blocklist, len(conf.blocklist_urls)]):

        log.debug('---------------------------------')
        log.verbose('Processing blocklists')

        # Prepare an empty blocklist
        new_blocklist = Blocklist()

        # Downloading the Snort blocklist?
        if conf.snort_blocklist:
            log.verbose(' - Downloading the Snort blocklist')
            try:
                new_blocklist.load_url(SNORT_BLOCKLIST_URL)
            except Exception as e:
                log.warning(f'Unable to download the Snort blocklist:  {e}')

        # ET blocklist?
        if conf.et_blocklist:
            log.verbose(' - Downloading the ET blocklist')
            try:
                new_blocklist.load_url(ET_BLOCKLIST_URL)
            except Exception as e:
                log.warning(f'Unable to download the ET blocklist:  {e}')

        # Any other blocklists
        for bl_url in conf.blocklist_urls:
            log.verbose(f' - Downloading blocklist:  {bl_url}')
            try:
                new_blocklist.load_url(bl_url)
            except Exception as e:
                log.warning(f'Unable to download blocklist:  {e}')

        # Compose the blocklist header and write the blocklist file
        blocklist_header = f'#-------------------------------------------------------------------\n'
        blocklist_header += f'# BLOCKLIST CREATED BY {SCRIPT_NAME.upper()} ON {conf.start_time}\n#\n'
        blocklist_header += f'# To Use this file, in your snort.lua, you need the following settings:\n'
        blocklist_header += f'# reputation = \n'
        blocklist_header += f'# {{\n'
        blocklist_header += f'#     blocklist = "{conf.blocklist_path}",\n'
        blocklist_header += f'#     ...\n'
        blocklist_header += f'# }}\n'
        blocklist_header += f'#\n#-------------------------------------------------------------------\n\n'

        log.info(f'Writing blocklist file to:  {conf.blocklist_path}')
        try:
            new_blocklist.write_file(conf.blocklist_path, blocklist_header)
        except Exception as e:
            log.warning(f'Unable to write blocklist:  {e}')

    # -----------------------------------------------------------------------------
    # Relad Snort

    # Have a PID file defined?
    if conf.defined('pid_path'):
        log.verbose(f'Loading Snort PID file: {conf.pid_path}')
        pid = 0
        try:
            with open(conf.pid_path, 'r') as f:
                pid = int(f.readline().strip())
        except Exception as e:
            log.warning(f'Error loading PID file {conf.pid_path}: {e}')

        if not pid:
            log.warning(f'Missing or invalid Snort PID: {pid}')
        else:
            log.info(f'Sending Snort process the reload signal (PID {pid}).')
            try:
                kill(pid, SIGHUP)
            except Exception as e:
                log.warning(f'Error sending SIGHUP to Snort3 process: {e}')

        # windows SIGHUP
        # import ctypes
        # ucrtbase = ctypes.CDLL('ucrtbase')
        # c_raise = ucrtbase['raise']
        # c_raise(some_signal)

    # Delete the working dir (if requested)
    del working_dir

    # -----------------------------------------------------------------------------
    # END Program Execution (main function)
    log.info('Program execution complete.')

# *****************************************************************************
# *****************************************************************************
#
#
#                       END OF MAIN FUNCTION
#
#
# *****************************************************************************
# *****************************************************************************


def flying_pig_banner():
    '''
    OMG We MUST HAVE FLYING PIGS! The community demands it.
    '''

    # For now simple printing, will need to clean this up
    # Pig art by JJ Cummings
    print(f"""
    https://github.com/shirkdog/pulledpork3
      _____ ____
     `----,\\    )   {VERSION_STR}
      `--==\\\\  /    {TAGLINE}
       `--==\\\\/
     .-~~~~-.Y|\\\\_  Copyright (C) 2021 Noah Dietrich, Colin Grady, Michael Shirk
  @_/        /  66\\_  and the PulledPork Team!
    |    \\   \\   _(\")
     \\   /-| ||'--'   Rules give me wings!
      \\_\\  \\_\\\\
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~""")


def parse_argv():
    '''
    Get command line arguments into global argparser variable
    '''

    # Parse command-line arguments
    arg_parser = ArgumentParser(description=f'{VERSION_STR} - {TAGLINE}')

    # we want Quiet or Verbose (v, vv), can't have more than one (but we can have none)
    group_verbosity = arg_parser.add_mutually_exclusive_group()
    group_verbosity.add_argument("-v", "--verbose", help="Increase output verbosity", action="store_true")
    group_verbosity.add_argument("-vv", "--debug", help="Really increase output verbosity", action="store_true")
    group_verbosity.add_argument("-q", "--quiet", help='Only display warnings and errors', action="store_true")

    # input file or folder (optional)
    group_input = arg_parser.add_mutually_exclusive_group()
    group_input.add_argument("-f", "--file", help="Use this file as source of rulesets", type=abspath)
    group_input.add_argument("-F", "--folder", help="Use all the tgz file in this folder as source of rulesets", type=abspath)

    # standard arguments
    arg_parser.add_argument("-c", "--configuration", help="path to the configuration file", nargs=1, type=abspath)
    arg_parser.add_argument("-V", "--version", help='Print version number and exit', action="store_true")
    arg_parser.add_argument("-k", "--keep-temp-dir", help='Do not delete the temp directory when done', action="store_true")
    arg_parser.add_argument("-po", "--print-oinkcode", help='Do not obfuscate oinkcode in output.', action="store_true")
    arg_parser.add_argument("-i", "--ignore-warn", help='Ignore warnings and continue processing.', action="store_true")

    return arg_parser.parse_args()


def print_operational_settings():
    '''
    Print all the operational settings after parsing (what we will do)
    '''

    log.verbose('---------------------------------')
    log.verbose("After parsing the command line and configuration file, this is what I know:")

    # halt-on-error
    if conf.args.ignore_warn:
        log.verbose('Warnings will not cause this program to terminate (damn the torpedos, full speed ahead!).')
    else:
        log.verbose('Program will terminate when encountering an error or warning.')

    # are we printing oinkcode?
    if conf.args.print_oinkcode:
        log.verbose('Oinkcode will NOT be obfuscated in the output (do not share your oinkcode).')
    else:
        log.verbose('Oinkcode will be obfuscated in the output (this is a good thing).')

    # Temp dir management
    log.verbose('Temporary directory is:  ' + conf.temp_path)

    if conf.delete_temp_path:
        log.verbose('Temporary working directory will be deleted at the end.')
    else:
        log.verbose('Temporary working directory will not be deleted at the end.')

    # env. variables
    log.verbose('The Snort version number used for processing is:  ' + conf.snort_version)
    if conf.defined('distro'):
        log.verbose('The distro used for processing is:  ' + conf.distro)
    log.verbose('The ips policy used for processing is:  ' + conf.ips_policy)

    if conf.defined('sorule_path'):
        log.verbose('Pre-compiled (.so) rules will be processed.')
        log.verbose('Pre-compiled (.so) files will be saved to:  ' + conf.sorule_path)
    else:
        log.verbose('Pre-compiled (.so) rules will not be processed.')
    # ruelset locations
    if conf.args.file:
        log.verbose('Rulesets will not be downloaded, they will be loaded from a single local file:  ' + "\n\t" + conf.args.file)
    elif conf.args.folder:
        log.verbose('Rulesets will not be downloaded, they will be loaded from all files in local folder:  ' + "\n\t" + conf.args.folder)
    else:
        log.verbose('Rulesets will be downloaded from: ')
        if conf.registered_ruleset:
            log.verbose("\tSnort Registered Ruleset")
        if conf.community_ruleset:
            log.verbose("\tSnort Community Ruleset")
        if conf.lightspd_ruleset:
            log.verbose("\tSnort LightSPD Ruleset")

    #   Rules
    if conf.ignored_files:
        log.verbose(f'The following rules files will not be included in rulesets:  {", ".join(conf.ignored_files)}')

    log.verbose("Rule Output mode is:  " + conf.rule_mode)
    if conf.rule_mode == 'policy':
        log.verbose('Policy file to write is:  ' + conf.policy_path)

    # local rules files
    for opt in conf.local_rules:
        log.verbose('Rules from Local rules file will be included:  ' + opt)

    log.verbose("All Rules will be written to a single file:  " + conf.rule_path)
    if conf.include_disabled_rules:
        log.verbose("Disabled rules will be written to the rules file")
    else:
        log.verbose("Disabled rules will not be written to the rules file")

    # policys
    log.verbose('The rule_mode is:  ' + conf.rule_mode)
    if conf.rule_mode == 'policy':
        log.verbose('the policy file written (to specify enabled rules) is:  ' + conf.policy_path)

    # blocklists
    if conf.snort_blocklist:
        log.verbose("Snort blocklist will be downloaded")
    if conf.et_blocklist:
        log.verbose("ET blocklist will be downloaded")

    for bl in conf.blocklist_urls:
        log.verbose("Other blocklist will be downloaded:  " + bl)

    if not any([conf.snort_blocklist, conf.et_blocklist, len(conf.blocklist_urls)]):
        log.verbose("No Blocklists will be downloaded.")
    else:
        log.verbose('Blocklist entries will be written to:  ' + conf.blocklist_path)

    # sid modification order
    log.verbose('The state_order is: ' + str(conf.state_order))

    # sid files:
    # check the sid files exist if defined
    if conf.defined('enablesid'):
        log.verbose(f'enablesid path is:  {conf.enablesid}')

    if conf.defined('dropsid'):
        log.verbose(f'dropsid path is:  {conf.dropsid}')

    if conf.defined('disablesid'):
        log.verbose(f'disablesid path is:  {conf.disablesid}')

    if conf.defined('modifysid'):
        log.verbose(f'modifysid path is:  {conf.modifysid}')

    # reload snort
    if conf.defined('pid_path'):
        log.verbose('Snort will be reloaded with new configuration, Pid loaded from:  ' + conf.pid_path)
    else:
        log.verbose('Snort will NOT be reloaded with new configuration.')


def extract_rulesets(files, target_dir):
    '''
    untar archives to folder,
    '''

    log.verbose(f'Preparing to extract rulesets:\n - Target Path:  {target_dir}')
    for file in files:

        # get the filename
        if file.filename.endswith('.tgz'):
            out_dir = join(target_dir, file.filename[:-4])
        elif file.filename.endswith('.tar.gz'):
            out_dir = join(target_dir, file.filename[:-7])
        else:
            out_dir = join(target_dir, file.filename)

        log.verbose(f' - Extracting archive:\n   - Filename: {file.filename}\n   - To: {out_dir}')
        file.extract(out_dir)


def print_environment(gc):
    '''
    Print environment Information
    '''

    # todo: get distro
    # todo: convert print to 'log'
    log.debug(f'Start time:  {gc.start_time}')
    log.verbose(f'Log level:  {log.level.name}')
    log.debug("Parsed command-line arguments are (including defaults):")
    for k, v in sorted(vars(gc.args).items()):
        log.debug(f' - {k} = {v}')
    log.debug(f'Platform:  {platform()}; {version()}')
    log.debug(f'uname:  {uname()}')
    log.debug(f'System:  {system()}')
    log.debug(f'Architecture:  {architecture()[0]}')
    log.debug(f'Python version:  {python_version()}')
    log.debug(f'PWD:  {environ.get("PWD")}')
    log.debug(f'Shell:  {environ.get("SHELL")}')
    log.debug(f'OS direcotry separator:  {sep}')


def get_snort_version(snort_path=None):
    '''
    Determine the Version of Snort
    '''

    log.debug('Determining Snort version from executable')

    # Default to just "snort" if no path provided
    snort_path = snort_path or 'snort'

    # Run snort to attempt to find the version
    command = f'{snort_path} -V'
    log.debug(f' - Running Snort using:  {command}')

    # call the snort binary with -V flag
    try:
        process = Popen(command, stdout=PIPE, stderr=PIPE, shell=True)
        output, error = process.communicate()
    except Exception as e:
        log.error(f'Fatal error running Snort:  {e}')

    # check return call for error
    if error:
        log.error(f'Fatal error running Snort:  [{process.returncode}] {error.strip()}')

    # parse stdout from snort binary to determine version number
    log.debug(f' - Output from Snort: \n{output}')
    x = search(r'Version ([-\.\d\w]+)', str(output))
    if not x:
        log.error('Unable to grok version number from Snort output')
    log.verbose(f' - Snort version is: {x[1]}')
    return x[1]


def normalize_version_number(number):

    log.debug(f'entering function normalize_version_number with param {number}')
    ver = ''
    # check for a semi-normal number first (n.n.n.n-n)
    if match(r"^\d+\.\d+\.\d+\.\d+-\d+$", number):
        ver = number.replace('-', '.', 1)

    # check for a semi-normal number (n.n.n.n)
    elif match(r"^\d+\.\d+\.\d+\.\d+$", number):
        ver = number + '.0'

    # check for early releases with poor numbering (n.n.n-n)
    elif match(r"^\d+\.\d+\.\d+-\d+$", number):
        ver = number.replace('-', '.0.', 1)

    else:
        log.warning(f'Unknown version number format: {number}')

    log.debug(f'Normalized version number is {ver}')
    return ver


def version_equal_or_lesser(v1, v2):
    # returns true if v1 is equal or less than v2

    log.debug(f'Entering Function version_equal_or_lesser(v1,v2), Comparing version strings: {v1} to {v2}')

    # This will split both the versions by '.'
    arr1 = v1.split(".")
    arr2 = v2.split(".")
    n = len(arr1)
    m = len(arr2)

    # converts to integer from string
    arr1 = [int(i) for i in arr1]
    arr2 = [int(i) for i in arr2]

    # compares which list is bigger and fills
    # smaller list with zero (for unequal delimeters)
    if n > m:
        for i in range(m, n):
            arr2.append(0)
    elif m > n:
        for i in range(n, m):
            arr1.append(0)

    for i in range(len(arr1)):
        if arr1[i] > arr2[i]:
            log.debug(f'- Returning True (lesser)')
            return True
        elif arr2[i] > arr1[i]:
            log.debug(f'- Returning False')
            return False

    log.debug(f'- Returning True (equal)')
    return True


def compile_so_rules(src_path, dst_path):
    log.debug(f'Entering function compile_so_rules with src_path: {src_path}')
    # the makefile & script to compile the .so rules is a bit of a mess, we need to clean it up a whole lot.

    # first we need to modify the generate_category.sh script to make it executable (755)
    gen_cat_script = join(src_path, 'generate_category.sh')
    log.debug(f'Changing permissions to 755 for {gen_cat_script}')
    try:
        chmod(gen_cat_script, 0o755)
    except Exception as e:
        log.error(f'Unable to chmod {gen_cat_script}:  {e}')

    # next we need to fix the makefile. it's a mess.
    # there are a number of hard-coded paths that don't match install standards.
    # we use pkg-config to determne the correct paths, then replace (regex) the
    # lines in the makefile.

    # fix makefile (PREFIX is hardcoded)
    # pkg-config --cflags snort = -I/usr/local/include/snort
    # pkg-config --modversion snort = 3.1.18.0
    # pkg-config --variable=bindir snort = /usr/local/bin

    # determine the 'bindir'
    command = 'pkg-config --variable=bindir snort'
    try:
        process = Popen(command, stdout=PIPE, stderr=PIPE, shell=True, universal_newlines=True)
        bindir, error = process.communicate()
    except Exception as e:
        log.error(f'Fatal error determining "bindir" by running {command}:  {e}')
    bindir = bindir.strip()
    if not bindir:
        log.error('"bindir" could not be determined by pkg-config.')
    bindir = join(bindir, 'snort')
    log.debug(f'bindir as calculated by pkg-config is: {bindir}')

    # determine the 'cflags'
    command = 'pkg-config --cflags snort'
    try:
        process = Popen(command, stdout=PIPE, stderr=PIPE, shell=True, universal_newlines=True)
        cflags, error = process.communicate()
    except Exception as e:
        log.error(f'Fatal error running {command}:  {e}')
    cflags = cflags.strip()

    if not cflags:
        log.error('"cflags" could not be determined by pkg-config.')

    log.debug(f'cflags as determined by pkg-config is: {cflags}')

    # Phasse 2: now we have to replace incorrect lines in makefile
    makefile = join(src_path, 'Makefile')
    log.debug(f'Preparing to replace incorrect lines in Makefile: {makefile}')

    with open(makefile, 'r+') as f:
        text = f.read()

        text = sub(r'CXXFLAGS \+= -I\$\(PREFIX\)/include/snort', f'CXXFLAGS += {cflags}', text, flags=MULTILINE)
        text = sub(r'\$\(SNORT\)', bindir, text, flags=MULTILINE)

        f.seek(0)
        f.write(text)
        f.truncate()

        # log.debug (f'The Modified makefile (for generating .so and .stub files ) is now: \n{text}\n')

    # now we run the makefile to generate the .so and .stub files in this directory
    log.info(f'Generating .so and .rule stub files. Be patient as this can take a few minutes.')
    try:
        process = Popen('make', stdout=PIPE, stderr=PIPE, shell=True, universal_newlines=True, cwd=src_path)
        command, error = process.communicate()
    except Exception as e:
        log.error(f'Fatal error running make to compile .so rules and generate stubs:  {e}')

    # todo: get line-by-line feedback as make runs

    log.debug(f'The output from running the makefile is: \n\n {command}\n')

    # assuming no problems, now we have .rules and .so files in this folder (yay!).
    # however, the .state files are not included, so we copy them from the pre-compiled rules folder
    # (not sure if that's ok, but we'll work with it)

    # copy .so files from our archive to working folder
    so_files = glob(join(src_path, '*.so'), recursive=False)

    for so_file in so_files:
        if isfile(so_file):
            copy(so_file, dst_path)
            log.debug(f'Copying .so file: {so_file}')

    # our rules (stub files) that we created are here
    lightspd_rules = Rules(src_path)
    # we use the .state files from the pre-compiled folder for the so rules
    lightspd_policies = Policies(join(src_path, '..', 'stubs'))

    # need to return these
    log.debug('after compiling .so rules:')
    log.debug(f' - SO Rules processed:  {lightspd_rules}')
    log.debug(f' - SO Policies processed:  {lightspd_policies}')

    return lightspd_rules, lightspd_policies


if __name__ == "__main__":
    main()
