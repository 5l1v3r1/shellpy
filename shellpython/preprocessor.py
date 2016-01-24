#!/usr/bin/env python
import os
import stat
import tempfile
import re
import getpass

spy_file_pattern = re.compile(r'(.*)\.spy$')
mod_time_pattern = re.compile('#mtime:(.*)')


def get_username():
    """Returns the name of current user. The function is used in construction of the path for processed shellpy files on
    temp file system

    :return: The name of current user
    """
    return getpass.getuser()
    # TODO: what if function does not work
    # TODO: see whether getpass is available everywhere


def preprocess_module(module_path):
    """The function compiles a module in shellpy to a python module, walking through all the shellpy files inside of
    the module and compiling all of them to python

    :param module_path: The path of module
    :return: The path of processed module
    """
    for item in os.walk(module_path):
        path, dirs, files = item
        for file in files:
            if spy_file_pattern.match(file):
                filepath = os.path.join(path, file)
                preprocess_file(filepath, is_root_script=False)

    return translate_to_temp_path(module_path)


def translate_to_temp_path(path):
    """Compiled shellpy files are stored on temp filesystem on path like this /{tmp}/{user}/{real_path_of_file_on_fs}
    Every user will have its own copy of compiled shellpy files. Since we store them somewhere else relative to
    the place where they actually are, we need a translation function that would allow us to easily get path
    of compiled file

    :param path: The path to be translated
    :return: The translated path
    """
    absolute_path = os.path.abspath(path)
    relative_path = os.path.relpath(absolute_path, os.path.abspath(os.sep))
    # TODO: this will not work in win where root is C:\ and absolute_in_path is on D:\
    translated_path = os.path.join(tempfile.gettempdir(), 'shellpy', get_username(), relative_path)
    return translated_path


def is_compilation_needed(in_filepath, out_filepath):
    """Shows whether compilation of input file is required. It may be not required if the output file did not change

    :param in_filepath: The path of shellpy file to be processed
    :param out_filepath: The path of the processed python file. It may exist or not.
    :return: True if compilation is needed, False otherwise
    """
    if not os.path.exists(out_filepath):
        return True

    in_mtime = os.path.getmtime(in_filepath)

    with open(out_filepath, 'r') as f:
        first_line = f.readline().strip()
        first_line_result = mod_time_pattern.search(first_line)
        if first_line_result:
            mtime = first_line_result.group(1)
            if str(in_mtime) == mtime:
                return False
        else:
            second_line = f.readline().strip()
            second_line_result = mod_time_pattern.search(second_line)
            if second_line_result:
                mtime = second_line_result.group(1)
                if str(in_mtime) == mtime:
                    return False
            else:
                raise RuntimeError("Either first or second line of file should contain source timestamp")

    return True


def get_header(filepath, is_root_script):
    """To execute converted shellpy file we need to add a header to it. The header contains needed imports and
    required code

    :param filepath: A shellpy file that is being converted. It is needed to get modification time of it and save it
    to the created python file. Then this modification time will be used to find out whether recompilation is needed
    :param is_root_script: Shows whether the file being processed is a root file, which means the one
            that user executed
    :return: data of the header
    """
    header_name = 'header_root.tpl' if is_root_script else 'header.tpl'
    header_filename = os.path.join(os.path.split(__file__)[0], header_name)

    with open(header_filename, 'r') as f:
        header_data = f.read()
        mod_time = os.path.getmtime(filepath)
        header_data = header_data.replace('{SOURCE_MODIFICATION_DATE}', 'mtime:{}'.format(mod_time))
        return header_data


def preprocess_file(in_filepath, is_root_script):
    """Coverts a single shellpy file to python

    :param in_filepath: The path of shellpy file to be processed
    :param is_root_script: Shows whether the file being processed is a root file, which means the one
            that user executed
    :return: The path of python file that was created of shellpy script
    """

    new_filepath = spy_file_pattern.sub(r"\1.py", in_filepath)
    out_filename = translate_to_temp_path(new_filepath)

    if not is_root_script and not is_compilation_needed(in_filepath, out_filename):
        # TODO: cache root also
        # TODO: if you don't compile but it's root, you need to change to exec
        return out_filename

    if not os.path.exists(os.path.dirname(out_filename)):
        os.makedirs(os.path.dirname(out_filename), mode=0700)

    header_data = get_header(in_filepath, is_root_script)
    out_file_data = header_data

    with open(in_filepath, 'r') as f:
        in_file_data = f.read()

        processed_data = in_file_data

        processed_data = process_multilines(processed_data)
        processed_data = process_long_lines(processed_data)
        processed_data = process_code_both(processed_data)
        processed_data = process_code_start(processed_data)

        processed_data = escape(processed_data)
        processed_data = intermediate_to_final(processed_data)

        out_file_data += processed_data

    with open(out_filename, 'w') as f:
        f.write(out_file_data)

    in_file_stat = os.stat(in_filepath)
    os.chmod(out_filename, in_file_stat.st_mode)

    if is_root_script:
        os.chmod(out_filename, in_file_stat.st_mode | stat.S_IEXEC)

    return out_filename


def process_multilines(script_data):
    """Converts a pyshell multiline expression to one line pyshell expression, each line of which is separated
    by semicolon. An example would be:
    f = `
    echo 1 > test.txt
    ls -l
    `

    :param script_data: the string of the whole script
    :return: the shellpy script with multiline expressions converted to intermediate form
    """
    code_multiline_pattern = re.compile(r'^([^`\n\r]*?)`\s*?$[\n\r]{1,2}(.*?)`\s*?$', re.MULTILINE | re.DOTALL)

    script_data = code_multiline_pattern.sub(r'\1multiline_shexe(\2)shexe', script_data)

    pattern = re.compile(r'multiline_shexe.*?shexe', re.DOTALL)

    strings = []
    processed_string = []
    for match in pattern.finditer(script_data):
        original_str = script_data[match.start():match.end()]
        processed_str = re.sub(r'([\r\n]{1,2})', r'; \\\1', original_str)

        strings.append(original_str)
        processed_string.append(processed_str)

    pairs = zip(strings, processed_string)
    for s, ps in pairs:
        script_data = script_data.replace(s, ps)

    return script_data


def process_long_lines(script_data):
    """Converts to python a pyshell expression that takes more than one line. An example would be:
    f = `echo The string \
        on several \
        lines

    :param script_data: the string of the whole script
    :return: the shellpy script converted to intermediate form
    """
    code_long_line_pattern = re.compile(r'`(((.*?\\\s*?$)[\n\r]{1,2})+(.*$))', re.MULTILINE)
    script_data = code_long_line_pattern.sub(r'longline_shexe(\1)shexe', script_data)
    return script_data


def process_code_both(script_data):
    """Converts to python a pyshell script that has ` symbol both in the beginning of expression and in the end.
    An example would be:
    f = `echo 1`

    :param script_data: the string of the whole script
    :return: the shellpy script converted to intermediate form
    """
    code_both_pattern = re.compile(r'`(.*?)`')
    script_data = code_both_pattern.sub(r'both_shexe(\1)shexe', script_data)
    return script_data


def process_code_start(script_data):
    """Converts to python a pyshell script that has ` symbol only in the beginning. An example would be:
    f = `echo 1

    :param script_data: the string of the whole script
    :return: the shellpy script converted to intermediate form
    """
    code_start_pattern = re.compile(r'^([^\n\r`]*)`([^`\n\r]+)$', re.MULTILINE)
    script_data = code_start_pattern.sub(r'\1start_shexe(\2)shexe', script_data)
    return script_data


def escape(script_data):
    """Escapes shell commands

    :param script_data: the string of the whole script
    :return: escaped script
    """
    pattern = re.compile(r'[a-z]*_shexe.*?shexe', re.DOTALL)

    strings = []
    processed_string = []

    for match in pattern.finditer(script_data):
        original_str = script_data[match.start():match.end()]
        if original_str.find('\'') != -1:
            processed_str = original_str.replace('\'', '\\\'')

            strings.append(original_str)
            processed_string.append(processed_str)

    pairs = zip(strings, processed_string)
    for s, ps in pairs:
        script_data = script_data.replace(s, ps)

    return script_data


def intermediate_to_final(script_data):
    """All shell blocks are first compiled to intermediate form. This part of code converts the intermediate
    to final python code

    :param script_data: the string of the whole script
    :return: python script ready to be executed
    """
    intermediate_pattern = re.compile(r'[a-z]*_shexe\((.*?)\)shexe', re.MULTILINE | re.DOTALL)
    return intermediate_pattern.sub(r"exe('\1'.format(**dict(locals(), **globals())))", script_data)