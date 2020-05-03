
import os
import sys
import logging
import concurrent.futures
from datetime import datetime
import argparse
import csv
import warnings
import numpy as np
import librosa

from koogu.data import FilenameExtensions, Process, Settings
from koogu.utils import instantiate_logging
from koogu.utils.terminal import ArgparseConverters
from koogu.utils.config import Config, ConfigError, datasection2dict, log_config
from koogu.utils.filesystem import restrict_classes_with_whitelist_file, AudioFileList

_program_name = 'prepare_data'
_matched_clips_dirname = 'annotated_clips'
_unmatched_clips_dirname = 'other_clips'
_default_audio_filetypes = ['.wav', '.WAV', '.flac', '.aif', '.mp3']


def from_selection_table_map(audio_settings, audio_seltab_list,
                             audio_root, seltab_root, output_root,
                             **kwargs):

    logger = logging.getLogger(__name__)

    # Discard invalid entries, if any
    valid_entries_mask = [
        (_validate_seltab_filemap_lhs(audio_root, lhs) and _validate_seltab_filemap_rhs(seltab_root, rhs))
        for (_, lhs, rhs) in audio_seltab_list]
    for e_idx in [idx for idx, e_mask in enumerate(valid_entries_mask) if not e_mask]:
        logger.error('Entry ({:s},{:s}) is invalid. Skipping...'.format(*audio_seltab_list[e_idx]))
    if sum(valid_entries_mask) == 0:
        print('Nothing to process')
        return

    input_generator = AudioFileList.from_annotations(
        [audio_seltab_list[e_idx] for e_idx, e_mask in enumerate(valid_entries_mask) if e_mask],
        audio_root, seltab_root,
        show_progress=kwargs.pop('show_progress') if 'show_progress' in kwargs else False)

    # Re-map parameter names and add defaults for any missing ones
    if 'positive_overlap_threshold' in kwargs:
        kwargs['min_selection_overlap_fraction'] = kwargs.pop('positive_overlap_threshold')
    if 'negative_overlap_threshold' in kwargs:
        kwargs['max_nonmatch_overlap_fraction'] = kwargs.pop('negative_overlap_threshold')

        if 'dest_nonmatch_subdir' not in kwargs:
            kwargs['dest_nonmatch_subdir'] = _unmatched_clips_dirname

    _batch_process(
        audio_settings, input_generator,
        audio_root, output_root,
        **kwargs)


def from_top_level_dirs(audio_settings, class_dirs,
                        audio_root, output_root,
                        **kwargs):

    input_generator = AudioFileList.from_directories(
        audio_root, class_dirs,
        kwargs.pop('filetypes') if 'filetypes' in kwargs else _default_audio_filetypes,
        show_progress=kwargs.pop('show_progress') if 'show_progress' in kwargs else False)

    _batch_process(
        audio_settings, input_generator,
        audio_root, output_root,
        **kwargs)


def _batch_process(audio_settings, input_generator,
                   audio_root, dest_root,
                   dest_nonmatch_subdir=None,  # If not None, will mean that we'll be saving seltab negatives
                   **kwargs):

    logger = logging.getLogger(__name__)

    # Warn about existing output directory
    if os.path.exists(dest_root) and os.path.isdir(dest_root):
        warnings.showwarning('Output directory {:s} already exists. Contents may get overwritten'.format(dest_root),
                             Warning, _program_name, '')

    if not os.path.exists(dest_root):
        os.makedirs(dest_root, exist_ok=True)

    audio_settings = Settings.Audio(**audio_settings)

    per_class_file_n_clip_counts = dict()

    t_start = datetime.now()
    logger.info('Started at: {}'.format(t_start))

    if dest_nonmatch_subdir is not None:   # Need to save negative clips too
        per_class_file_n_clip_counts[dest_nonmatch_subdir] = [0, 0]

    def handle_outcome(future_h, a_class, a_file, num_annots):   # internal use utility function
        try:
            num_pos_clips, num_neg_clips = future_h.result()
        except Exception as ho_exc:
            logger.error('Processing file {:s} generated an exception: {:s}'.format(repr(audio_file), repr(ho_exc)))
        else:
            if num_annots is not None:   # Seltabs were available
                if num_neg_clips is not None:   # Negative clips were written too
                    logger.info('{:s}: {:d} annotations. Wrote {:d} matching & {:d} non-matching clips'.format(
                        a_file, num_annots, num_pos_clips, num_neg_clips))
                else:
                    logger.info('{:s}: {:d} annotations. Wrote {:d} matching clips'.format(
                        a_file, num_annots, num_pos_clips))
            else:   # Top-level directories were processed
                logger.info('{:s}: Wrote {:d} clips'.format(a_file, num_pos_clips))

            if a_class in per_class_file_n_clip_counts:     # increment counts
                per_class_file_n_clip_counts[a_class][0] += 1 if num_pos_clips > 0 else 0
                per_class_file_n_clip_counts[a_class][1] += num_pos_clips
            else:                                           # make new class entry
                per_class_file_n_clip_counts[a_class] = [1 if num_pos_clips > 0 else 0, num_pos_clips]

            if dest_nonmatch_subdir is not None:    # Increment negative clip count
                per_class_file_n_clip_counts[dest_nonmatch_subdir][0] += 1 if num_neg_clips > 0 else 0
                per_class_file_n_clip_counts[dest_nonmatch_subdir][1] += num_neg_clips

    file_min_dur = float(audio_settings.clip_length) / float(audio_settings.fs)     # Set to clip duration
    file_max_dur = kwargs.pop('max_file_duration') if 'max_file_duration' in kwargs else np.inf

    num_workers = kwargs.pop('num_threads') if 'num_threads' in kwargs else os.cpu_count()
    futures_dict = {}
    # Keep up to 3 * num_workers in futures_dict so that in cases where there are very many files to process, the size
    # of futures_dict (and in turn the queuing mechanism in concurrent.futures) doesn't end up hindering memory use.
    # Continue adding more items as and when processing of previous items complete.
    with concurrent.futures.ProcessPoolExecutor(max_workers=num_workers) as executor:
        for class_name, audio_file, annotations in input_generator:

            audio_file_fullpath = os.path.join(audio_root, audio_file)

            # If file is too short or too long, discard and continue to next
            file_dur = librosa.get_duration(filename=audio_file_fullpath)
            if not (file_min_dur < file_dur <= file_max_dur):
                logger.warning('%s: duration = %f s. Ignoring.', repr(audio_file_fullpath), file_dur)
                continue

            # Derive destination paths. Create directories as necessary
            rel_path, filename = os.path.split(audio_file)
            target_filename = filename + FilenameExtensions.numpy
            target_dir = os.path.join(dest_root, class_name, rel_path) if annotations is not None \
                else os.path.join(dest_root, rel_path)  # If seltab not given, then class name was already in the path
            os.makedirs(target_dir, exist_ok=True)
            dest_nonmatch_dir = None
            if dest_nonmatch_subdir is not None and annotations is not None:
                dest_nonmatch_dir = os.path.join(dest_root, dest_nonmatch_subdir, rel_path)
                os.makedirs(dest_nonmatch_dir, exist_ok=True)

            # If many items are already currently being processed, wait for and handle one finished result before adding
            # a new one to processing queue.
            if len(futures_dict) >= num_workers * 3:
                # Using a loop here only because it is safe. Will 'break' out after the first item anyways.
                del_future_h = None
                for future in concurrent.futures.as_completed(futures_dict):
                    handle_outcome(future, *futures_dict[future])

                    # Copy handle for deletion and get out
                    del_future_h = future
                    break

                # Deleting outside of the above loop, coz it may be unsafe to do so while in a generator
                if del_future_h is not None:
                    del futures_dict[del_future_h]

            # Now add to the processing queue
            futures_dict[executor.submit(Process.audio2clips,
                                         audio_settings,
                                         audio_file_fullpath,
                                         os.path.join(target_dir, target_filename),
                                         selections=annotations,
                                         non_matching_clips_file=None if dest_nonmatch_dir is None \
                                             else os.path.join(dest_nonmatch_dir, target_filename),
                                         **kwargs)] = \
                (class_name, audio_file, None if annotations is None else annotations.shape[0])

        # Now wait for the trailing futures
        for future in concurrent.futures.as_completed(futures_dict):
            handle_outcome(future, *futures_dict[future])

    t_end = datetime.now()
    logger.info('Finished at: {}'.format(t_end))

    logging.info('Processing time (hh:mm:ss.ms) {}'.format(t_end - t_start))
    logging.shutdown()

    print('{:<55s} - {:>5s}, {:>6s}'.format('Class', 'Files', 'Clips'))
    for class_name, class_counts in per_class_file_n_clip_counts.items():
        print('{:<55s} - {:>5d}, {:>6d}'.format(class_name, class_counts[0], class_counts[1]))

    print('Processing time (hh:mm:ss.ms) {}'.format(t_end - t_start))


def _instantiate_logging(args, audio_settings):
    # Create the logger
    logging.basicConfig(filename=args.log if args.log is not None else os.path.join(args.dst, _program_name + '.log'),
                        filemode='w', level=args.loglevel, format='[%(levelname).1s] %(funcName)s: %(message)s')

    logger = logging.getLogger(__name__)

    logger.info('Command-line arguments: {}'.format({k: v for k, v in vars(args).items() if v is not None}))
    logger.info('Audio settings: {}'.format(audio_settings))


# def _audio_settings_from_config(cfg_file):
#     """Load audio settings parameters from the config file and return a Settings.Audio instance"""
#
#     cfg = Config(cfg_file, 'DATA')
#
#     audio_settings = {
#         'clip_length': cfg.DATA.audio_clip_length,
#         'clip_advance': cfg.DATA.audio_clip_advance,
#         'desired_fs': cfg.DATA.audio_fs,
#         'filterspec': cfg.DATA.audio_filterspec
#     }
#
#     # Validate settings
#     _ = Settings.Audio(**audio_settings)    # Will throw, if failure. Will be caught by caller
#
#     return audio_settings


def _validate_seltab_filemap_lhs(audio_root, entry):
    return len(entry) > 0 and os.path.exists(os.path.join(audio_root, entry))


def _validate_seltab_filemap_rhs(seltab_root, entry):
    return len(entry) > 0 and \
           os.path.isfile(os.path.join(seltab_root, entry) if seltab_root is not None else entry)


__all__ = [from_selection_table_map, from_top_level_dirs]


if __name__ == '__main__':

    parser = argparse.ArgumentParser(prog=_program_name, allow_abbrev=False,
                                     description='Prepare audio data before their conversion to TFRecords.')
    parser.add_argument('cfg', metavar='<CONFIG FILE>',
                        help='Path to config file.')
    parser.add_argument('src', metavar='<AUDIO SOURCE>',
                        help='Path to either a single audio file or to a directory. When a directory, if selection ' +
                             'table info is also provided (using \'selmap\'), then this must be the root path from ' +
                             'which relative paths to audio files in the selection tables will be resolved. ' +
                             'Otherwise, this must be the root directory containing per-class top-level ' +
                             'subdirectories which in turn contain audio files.')
    parser.add_argument('dst', metavar='<DST DIRECTORY>',
                        help='Path to destination directory into which prepared data will be written.')
    parser.add_argument('--whitelist', metavar='FILE',
                        help='Path to text file containing names (one per line) of whitelisted classes.')
    arg_group_seltab = parser.add_argument_group('Selection tables',
                                                 'Control which sections of audio files are retained in the output, ' +
                                                 'with the use of Raven selection tables.')
    arg_group_seltab.add_argument('--selmap', metavar='CSVFILE',
                                  help='Path to csv file containing one-to-one mappings from audio files to selection' +
                                  ' table files. Audio filepaths must be relative to <AUDIO_SOURCE>. If selection ' +
                                  'table files are not absolute paths, use \'selroot\' to specify the root directory ' +
                                  'path.')
    arg_group_seltab.add_argument('--selroot', metavar='DIRECTORY',
                                  help='Path to the root directory containing selection table files. Note that, if ' +
                                  'this is specified, all selection table paths in \'selmap\' file be treated as ' +
                                  'relative paths.')
    arg_group_seltab.add_argument('--accept-thld', metavar='0-100', type=ArgparseConverters.valid_percent,
                                  default=90., dest='seltab_accept_thld',
                                  help='Clips from the source audio files are retained in the output only if the ' +
                                  'percentage of their temporal overlap with any annotation in a matched selection ' +
                                  'table is above this threshold value. Default: 90%%.')
    arg_group_seltab.add_argument('--save-reject-class', dest='save_reject_class', action='store_true',
                                  help='Enable saving of clips that do not match annotations as \'other\' class. ' +
                                       'Default: False.')
    arg_group_seltab.add_argument('--reject-thld', metavar='0-100', type=ArgparseConverters.valid_percent,
                                  default=0., dest='seltab_reject_thld',
                                  help='Clips from the source audio files are retained in the output \'other\' class ' +
                                       'only if the percentage of their temporal overlap with any annotation in a ' +
                                       'matched selection table is under this threshold value. Default: 0%%.')
    arg_group_prctrl = parser.add_argument_group('Process control')
    arg_group_prctrl.add_argument('--threads', metavar='NUM', type=ArgparseConverters.positive_integer,
                                  help='Number of threads to spawn for parallel execution (default: as many CPUs).')
    arg_group_logging = parser.add_argument_group('Logging')
    arg_group_logging.add_argument('--log', metavar='FILE',
                                   help='Path to file to which logs will be written out.')
    arg_group_logging.add_argument('--loglevel', choices=['CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG'],
                                   default='INFO',
                                   help='Logging level.')
    arg_group_misc = parser.add_argument_group('Miscellaneous')
    arg_group_misc.add_argument('--filetypes', metavar='EXTN', nargs='+', default=_default_audio_filetypes,
                                help='Audio file types to restrict processing to. Option is ignored if processing ' +
                                     'selection tables or a single file. Can specify multiple types separated by ' +
                                     'whitespaces. By default, will include for processing all discovered files with ' +
                                     'the following extensions: ' + ', '.join(_default_audio_filetypes))
    arg_group_misc.add_argument('--maxdur', metavar='SECONDS', dest='max_file_duration', type=float,
                                help='Maximum duration of an audio file to consider it for processing. Larger files ' +
                                     'will be ignored. Default: no limit.')
    args = parser.parse_args()

    if not os.path.exists(args.src):
        print('Error: Invalid source specified', file=sys.stderr)
        exit(2)

    try:
        data_settings = datasection2dict(Config(args.cfg, 'DATA').DATA)
    except FileNotFoundError as exc:
        print('Error loading config file: {}'.format(exc.strerror), file=sys.stderr)
        exit(exc.errno)
    except ConfigError as exc:
        print('Error processing config file: {}'.format(str(exc)), file=sys.stderr)
        exit(1)
    except Exception as exc:
        print('Error processing config file: {}'.format(repr(exc)), file=sys.stderr)
        exit(1)

    if os.path.isfile(args.src):    # If src is an audio file by itself. Process and exit immediately.

        # Warn about ignoring whitelist and seltab info, if also provided
        if args.selmap is not None:
            warnings.showwarning('Processing a single file, will ignore \'selmap\'.', Warning, _program_name, '')
        if args.whitelist is not None:
            warnings.showwarning('Processing a single file, will ignore \'whitelist\'.', Warning, _program_name, '')

        num_clips, _ = Process.audio2clips(Settings.Audio(**data_settings['audio_settings']), args.src, args.dst)

        print('{:s}: {:d} clips'.format(os.path.split(args.src)[-1], num_clips[0]))

        exit(0)

    other_args = {'show_progress': False}
    if args.max_file_duration:
        other_args['max_file_duration'] = args.max_file_duration
    if args.threads:
        other_args['num_threads'] = args.threads

    instantiate_logging(args.log if args.log is not None else
                            os.path.join(args.dst, _program_name + '.log'),
                        args.loglevel, args)
    log_config(logging.getLogger(__name__), data_cfg={'audio_settings': data_settings['audio_settings']})

    exit_code = 0

    if args.selmap is not None:   # If selmap file is given, build a container with all relevant info

        with open(args.selmap, 'r', newline='') as f:
            seltab_filemap = [(entry[0], entry[1], entry[2])
                              for entry in csv.reader(f, delimiter=',', quoting=csv.QUOTE_NONE)
                              if len(entry) == 3]
        if len(seltab_filemap) == 0:
            print('No (valid) mappings found in {:s}'.format(args.selmap), file=sys.stderr)

        else:

            other_args['positive_overlap_threshold'] = args.seltab_accept_thld / 100
            if args.save_reject_class:
                other_args['negative_overlap_threshold'] = args.seltab_reject_thld / 100

            # Warn about ignoring whitelist, if also provided
            if args.whitelist is not None:
                warnings.showwarning('Will ignore \'whitelist\' because \'selmap\' is also provided.',
                                     Warning, _program_name, '')

            from_selection_table_map(data_settings['audio_settings'],
                                     audio_seltab_list=seltab_filemap,
                                     audio_root=args.src,
                                     seltab_root=args.selroot,
                                     output_root=args.dst,
                                     **other_args)

    else:
        # If seltab info wasn't available, build the list of classes from the combination of the dir listing of src and
        # classes whitelist.

        # List of classes (first level directory names)
        try:
            class_dirs = sorted([c for c in os.listdir(args.src) if os.path.isdir(os.path.join(args.src, c))])
        except FileNotFoundError as exc:
            print('Error reading source directory: {}'.format(exc.strerror), file=sys.stderr)
            exit_code = exc.errno
        else:

            if args.whitelist is not None:  # Apply whitelist
                class_dirs = restrict_classes_with_whitelist_file(class_dirs, args.whitelist)
                print('Application of whitelist from {:s} results in {:d} classes.'.format(
                    args.whitelist, len(class_dirs)))

            if len(class_dirs) == 0:
                print('No classes to process.')

            else:

                if args.filetypes:
                    other_args['filetypes'] = args.filetypes

                from_top_level_dirs(data_settings['audio_settings'], class_dirs, args.src, args.dst, **other_args)

    logging.shutdown()

    exit(exit_code)
