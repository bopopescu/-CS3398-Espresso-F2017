# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""gcloud CLI tree generators for non-gcloud CLIs.

A CLI tree for a supported command is generated by using the root command plus
`help` or `--help` arguments to do a DFS traversal. Each node is generated
from a man-ish style runtime document.

Supported CLI commands have their own runtime help document quirks, so each is
handled by an ad-hoc parser. The parsers rely on consistency within commands
and between command releases.

The CLI tree for an unsupported command is generated from the output of
`man the-command` and contains only the command root node.
"""

import abc
import json
import os
import re
import subprocess
import textwrap

from googlecloudsdk.calliope import cli_tree
from googlecloudsdk.core import exceptions
from googlecloudsdk.core import log
from googlecloudsdk.core.console import progress_tracker
from googlecloudsdk.core.resource import resource_printer
from googlecloudsdk.core.util import encoding
from googlecloudsdk.core.util import files
from googlecloudsdk.core.util import text as text_utils


class NoCliTreeGeneratorForCommand(exceptions.Error):
  """Command does not have a CLI tree generator."""


def _NormalizeSpace(text):
  """Returns text dedented and multiple non-indent spaces replaced by one."""
  return re.sub('([^ ])   *', r'\1 ', textwrap.dedent(text)).strip('\n')


def _Flag(name, description='', value=None, default=None, type_='string',
          category='', is_global=False, is_required=False):
  """Initializes and returns a flag dict node."""
  return {
      cli_tree.LOOKUP_ATTR: {},
      cli_tree.LOOKUP_CATEGORY: category,
      cli_tree.LOOKUP_DEFAULT: default,
      cli_tree.LOOKUP_DESCRIPTION: _NormalizeSpace(description),
      cli_tree.LOOKUP_GROUP: '',
      cli_tree.LOOKUP_IS_GLOBAL: is_global,
      cli_tree.LOOKUP_IS_HIDDEN: False,
      cli_tree.LOOKUP_IS_REQUIRED: is_required,
      cli_tree.LOOKUP_NAME: name,
      cli_tree.LOOKUP_VALUE: value,
      cli_tree.LOOKUP_TYPE: type_,
  }


def _Positional(name, description='', default=None, nargs='0'):
  """Initializes and returns a positional dict node."""
  return {
      cli_tree.LOOKUP_DEFAULT: default,
      cli_tree.LOOKUP_DESCRIPTION: _NormalizeSpace(description),
      cli_tree.LOOKUP_NAME: name,
      cli_tree.LOOKUP_NARGS: nargs,
  }


def _Command(path):
  """Initializes and returns a command/group dict node."""
  return {
      cli_tree.LOOKUP_CAPSULE: '',
      cli_tree.LOOKUP_COMMANDS: {},
      cli_tree.LOOKUP_FLAGS: {},
      cli_tree.LOOKUP_GROUPS: {},
      cli_tree.LOOKUP_IS_GROUP: False,
      cli_tree.LOOKUP_IS_HIDDEN: False,
      cli_tree.LOOKUP_PATH: path,
      cli_tree.LOOKUP_POSITIONALS: [],
      cli_tree.LOOKUP_RELEASE: 'GA',
      cli_tree.LOOKUP_SECTIONS: {},
  }


class CliTreeGenerator(object):
  """Base CLI tree generator."""

  def __init__(self, command):
    path, self.cli_name = os.path.split(command)
    self._cli_path = path or None
    self._cli_version = None  # For memoizing GetVersion()

  def Run(self, cmd):
    """Runs cmd and returns the output as a string."""
    return subprocess.check_output(cmd)

  def CliCommandExists(self):
    if not self._cli_path:
      self._cli_path = files.FindExecutableOnPath(self.cli_name)
    return self._cli_path

  def GetVersion(self):
    """Returns the CLI_VERSION string."""
    if not self._cli_version:
      self._cli_version = self.Run([self.cli_name, 'version']).split()[-1]
    return self._cli_version

  @abc.abstractmethod
  def GenerateTree(self):
    """Generates and returns the CLI tree dict."""
    return None

  def FindTreeFile(self, directories):
    """Returns (path,f) open for read for the first CLI tree in directories."""
    for directory in directories:
      path = os.path.join(directory or '.', self.cli_name) + '.json'
      try:
        return path, open(path, 'r')
      except IOError:
        pass
    return path, None

  def LoadOrGenerate(self, directories, verbose=False,
                     warn_on_exceptions=False):
    """Loads the CLI tree or generates it if it's out of date."""
    if not self.CliCommandExists():
      if verbose:
        log.warn(u'Command [{}] not found.'.format(self.cli_name))
      return None
    up_to_date = False
    path, f = self.FindTreeFile(directories)
    if f:
      with f:
        try:
          tree = json.load(f)
        except ValueError:
          # Corrupt JSON -- could have been interrupted.
          tree = None
        if tree:
          version = self.GetVersion()
          up_to_date = tree.get(cli_tree.LOOKUP_CLI_VERSION) == version
    if up_to_date:
      if verbose:
        log.status.Print(u'[{}] CLI tree version [{}] is up to date.'.format(
            self.cli_name, version))
      return tree
    with progress_tracker.ProgressTracker(
        u'{} the [{}] CLI tree'.format(
            'Updating' if f else 'Generating', self.cli_name)):
      tree = self.GenerateTree()
      try:
        f = open(path, 'w')
      except IOError as e:
        if not warn_on_exceptions:
          raise
        log.warn(str(e))
      else:
        with f:
          resource_printer.Print(tree, print_format='json', out=f)


class _BqCollector(object):
  """bq help document section collector."""

  def __init__(self, text):
    self.text = text.split('\n')
    self.heading = 'DESCRIPTION'
    self.lookahead = None
    self.ignore_trailer = False

  def Collect(self, strip_headings=False):
    """Returns the heading and content lines from text."""
    content = []
    if self.lookahead:
      if not strip_headings:
        content.append(self.lookahead)
      self.lookahead = None
    heading = self.heading
    self.heading = None
    while self.text:
      line = self.text.pop(0)
      if line.startswith(' ') or not strip_headings and not self.ignore_trailer:
        content.append(line.rstrip())
    while content and not content[0]:
      content.pop(0)
    while content and not content[-1]:
      content.pop()
    self.ignore_trailer = True
    return heading, content


class BqCliTreeGenerator(CliTreeGenerator):
  """bq CLI tree generator."""

  def Run(self, cmd):
    """Runs cmd and returns the output as a string."""
    try:
      output = subprocess.check_output(cmd)
    except subprocess.CalledProcessError as e:
      # bq exit code is 1 for help and --help. How do you know if help failed?
      if e.returncode != 1:
        raise
      output = e.output
    return output.replace('bq.py', 'bq')

  def AddFlags(self, command, content, is_global=False):
    """Adds flags in content lines to command."""
    while content:
      line = content.pop(0)
      name, description = line.strip().split(':', 1)
      paragraph = [description.strip()]
      default = ''
      while content and not content[0].startswith('  --'):
        line = content.pop(0).strip()
        if line.startswith('(default: '):
          default = line[10:-1]
        else:
          paragraph.append(line)
      description = ' '.join(paragraph).strip()
      if name.startswith('--[no]'):
        name = '--' + name[6:]
        type_ = 'bool'
        value = ''
      else:
        value = 'VALUE'
        type_ = 'string'
      command[cli_tree.LOOKUP_FLAGS][name] = _Flag(
          name=name,
          description=description,
          type_=type_,
          value=value,
          default=default,
          is_required=False,
          is_global=is_global,
      )

  def SubTree(self, path):
    """Generates and returns the CLI subtree rooted at path."""
    command = _Command(path)
    command[cli_tree.LOOKUP_IS_GROUP] = True
    text = self.Run([path[0], 'help'] + path[1:])

    # `bq help` lists help for all commands. Command flags are "defined"
    # by example. We don't attempt to suss that out.
    content = text.split('\n')
    while content:
      line = content.pop(0)
      if not line or not line[0].islower():
        continue
      name, text = line.split(' ', 1)
      description = [text.strip()]
      examples = []
      arguments = []
      paragraph = description
      while content and (not content[0] or not content[0][0].islower()):
        line = content.pop(0).strip()
        if line == 'Arguments:':
          paragraph = arguments
        elif line == 'Examples:':
          paragraph = examples
        else:
          paragraph.append(line)
      subcommand = _Command(path + [name])
      command[cli_tree.LOOKUP_COMMANDS][name] = subcommand
      if description:
        subcommand[cli_tree.LOOKUP_SECTIONS]['DESCRIPTION'] = '\n'.join(
            description)
      if examples:
        subcommand[cli_tree.LOOKUP_SECTIONS]['EXAMPLES'] = '\n'.join(
            examples)

    return command

  def GenerateTree(self):
    """Generates and returns the CLI tree rooted at self.cli_name."""

    # Construct the tree minus the global flags.
    tree = self.SubTree([self.cli_name])

    # Add the global flags to the root.
    text = self.Run([self.cli_name, '--help'])
    collector = _BqCollector(text)
    _, content = collector.Collect(strip_headings=True)
    self.AddFlags(tree, content, is_global=True)

    # Finally add the version stamps.
    tree[cli_tree.LOOKUP_CLI_VERSION] = self.GetVersion()
    tree[cli_tree.LOOKUP_VERSION] = cli_tree.VERSION

    return tree


class _GsutilCollector(object):
  """gsutil help document section collector."""

  UNKNOWN, ROOT, MAN, TOPIC = range(4)

  def __init__(self, text):
    self.text = text.split('\n')
    self.heading = 'CAPSULE'
    self.page_type = self.UNKNOWN

  def Collect(self, strip_headings=False):
    """Returns the heading and content lines from text."""
    content = []
    heading = self.heading
    self.heading = None
    while self.text:
      line = self.text.pop(0)
      if self.page_type == self.UNKNOWN:
        # The first heading distinguishes the document page type.
        if line.startswith('Usage:'):
          self.page_type = self.ROOT
          continue
        elif line == 'NAME':
          self.page_type = self.MAN
          heading = 'CAPSULE'
          continue
        elif not line.startswith(' '):
          continue
      elif self.page_type == self.ROOT:
        # The root help page.
        if line == 'Available commands:':
          heading = 'COMMANDS'
          continue
        elif line == 'Additional help topics:':
          self.heading = 'TOPICS'
          break
        elif not line.startswith(' '):
          continue
      elif self.page_type == self.MAN:
        # A command/subcommand man style page.
        if line == 'OVERVIEW':
          self.page_type = self.TOPIC
          self.heading = 'DESCRIPTION'
          break
        elif line == 'SYNOPSIS':
          self.heading = line
          break
        elif line.endswith('OPTIONS'):
          self.heading = 'FLAGS'
          break
        elif line and line[0].isupper():
          self.heading = line.split(' ', 1)[-1]
          break
      elif self.page_type == self.TOPIC:
        # A topic man style page.
        if line and line[0].isupper():
          self.heading = line
          break
      if line.startswith(' ') or not strip_headings:
        content.append(line.rstrip())
    while content and not content[0]:
      content.pop(0)
    while content and not content[-1]:
      content.pop()
    return heading, content


class GsutilCliTreeGenerator(CliTreeGenerator):
  """gsutil CLI tree generator."""

  def __init__(self, *args, **kwargs):
    super(GsutilCliTreeGenerator, self).__init__(*args, **kwargs)
    self.topics = []

  def Run(self, cmd):
    """Runs the command in cmd and returns the output as a string."""
    try:
      output = subprocess.check_output(cmd)
    except subprocess.CalledProcessError as e:
      # gsutil exit code is 1 for --help depending on the context.
      if e.returncode != 1:
        raise
      output = e.output
    return output

  def AddFlags(self, command, content, is_global=False):
    """Adds flags in content lines to command."""

    def _Add(name, description):
      value = ''
      type_ = 'bool'
      default = ''
      command[cli_tree.LOOKUP_FLAGS][name] = _Flag(
          name=name,
          description=description,
          type_=type_,
          value=value,
          default=default,
          is_required=False,
          is_global=is_global,
      )

    parse = re.compile(' *((-[^ ]*,)* *(-[^ ]*) *)(.*)')
    name = None
    description = []
    for line in content:
      if line.startswith('  -'):
        if name:
          _Add(name, '\n'.join(description))
        match = parse.match(line)
        name = match.group(3)
        description = [match.group(4).rstrip()]
      elif len(line) > 16:
        description.append(line[16:].rstrip())
    if name:
      _Add(name, '\n'.join(description))

  def SubTree(self, path):
    """Generates and returns the CLI subtree rooted at path."""
    command = _Command(path)
    is_help_command = len(path) > 1 and path[1] == 'help'
    if is_help_command:
      cmd = path
    else:
      cmd = path + ['--help']
    text = self.Run(cmd)
    collector = _GsutilCollector(text)

    while True:
      heading, content = collector.Collect()
      if not heading:
        break
      elif heading == 'CAPSULE':
        if content:
          command[cli_tree.LOOKUP_CAPSULE] = content[0].split('-', 1)[1].strip()
      elif heading == 'COMMANDS':
        if is_help_command:
          continue
        for line in content:
          try:
            name = line.split()[0]
          except IndexError:
            continue
          if name == 'update':
            continue
          command[cli_tree.LOOKUP_IS_GROUP] = True
          command[cli_tree.LOOKUP_COMMANDS][name] = self.SubTree(path + [name])
      elif heading == 'FLAGS':
        self.AddFlags(command, content)
      elif heading == 'SYNOPSIS':
        commands = []
        for line in content:
          if not line:
            break
          cmd = line.split()
          if len(cmd) <= len(path):
            continue
          if cmd[:len(path)] == path:
            name = cmd[len(path)]
            if name[0].islower() and name not in ('off', 'on', 'false', 'true'):
              commands.append(name)
        if len(commands) > 1:
          command[cli_tree.LOOKUP_IS_GROUP] = True
          for name in commands:
            command[cli_tree.LOOKUP_COMMANDS][name] = self.SubTree(
                path + [name])
      elif heading == 'TOPICS':
        for line in content:
          try:
            self.topics.append(line.split()[0])
          except IndexError:
            continue
      elif heading.isupper():
        if heading.lower() == path[-1]:
          heading = 'DESCRIPTION'
        command[cli_tree.LOOKUP_SECTIONS][heading] = '\n'.join(
            [line[2:] for line in content])

    return command

  def GenerateTree(self):
    """Generates and returns the CLI tree rooted at self.cli_name."""
    tree = self.SubTree([self.cli_name])

    # Add the global flags to the root.
    text = self.Run([self.cli_name, 'help', 'options'])
    collector = _GsutilCollector(text)
    while True:
      heading, content = collector.Collect()
      if not heading:
        break
      if heading == 'FLAGS':
        self.AddFlags(tree, content, is_global=True)

    # Add the help topics.
    help_command = tree[cli_tree.LOOKUP_COMMANDS]['help']
    help_command[cli_tree.LOOKUP_IS_GROUP] = True
    for topic in self.topics:
      help_command[cli_tree.LOOKUP_COMMANDS][topic] = self.SubTree(
          help_command[cli_tree.LOOKUP_PATH] + [topic])

    # Finally add the version stamps.
    tree[cli_tree.LOOKUP_CLI_VERSION] = self.GetVersion()
    tree[cli_tree.LOOKUP_VERSION] = cli_tree.VERSION

    return tree


class _KubectlCollector(object):
  """Kubectl help document section collector."""

  def __init__(self, text):
    self.text = text.split('\n')
    self.heading = 'DESCRIPTION'
    self.lookahead = None
    self.ignore_trailer = False

  def Collect(self, strip_headings=False):
    """Returns the heading and content lines from text."""
    content = []
    if self.lookahead:
      if not strip_headings:
        content.append(self.lookahead)
      self.lookahead = None
    heading = self.heading
    self.heading = None
    while self.text:
      line = self.text.pop(0)
      usage = 'Usage:'
      if line.startswith(usage):
        line = line[len(usage):].strip()
        if line:
          self.lookahead = line
        self.heading = 'USAGE'
        break
      if line.endswith(':'):
        if 'Commands' in line:
          self.heading = 'COMMANDS'
          break
        if 'Examples' in line:
          self.heading = 'EXAMPLES'
          break
        if 'Options' in line:
          self.heading = 'FLAGS'
          break
      if line.startswith(' ') or not strip_headings and not self.ignore_trailer:
        content.append(line.rstrip())
    while content and not content[0]:
      content.pop(0)
    while content and not content[-1]:
      content.pop()
    self.ignore_trailer = True
    return heading, content


class KubectlCliTreeGenerator(CliTreeGenerator):
  """kubectl CLI tree generator."""

  def AddFlags(self, command, content, is_global=False):
    """Adds flags in content lines to command."""
    for line in content:
      flags, description = line.strip().split(':', 1)
      flag = flags.split(', ')[-1]
      name, value = flag.split('=')
      if value in ('true', 'false'):
        value = ''
        type_ = 'bool'
      else:
        value = 'VALUE'
        type_ = 'string'
      default = ''
      command[cli_tree.LOOKUP_FLAGS][name] = _Flag(
          name=name,
          description=description,
          type_=type_,
          value=value,
          default=default,
          is_required=False,
          is_global=is_global,
      )

  def SubTree(self, path):
    """Generates and returns the CLI subtree rooted at path."""
    command = _Command(path)
    text = self.Run(path + ['--help'])
    collector = _KubectlCollector(text)

    while True:
      heading, content = collector.Collect()
      if not heading:
        break
      elif heading == 'COMMANDS':
        for line in content:
          try:
            name = line.split()[0]
          except IndexError:
            continue
          command[cli_tree.LOOKUP_IS_GROUP] = True
          command[cli_tree.LOOKUP_COMMANDS][name] = self.SubTree(path + [name])
      elif heading in ('DESCRIPTION', 'EXAMPLES'):
        command[cli_tree.LOOKUP_SECTIONS][heading] = '\n'.join(content)
      elif heading == 'FLAGS':
        self.AddFlags(command, content)
    return command

  def GetVersion(self):
    """Returns the CLI_VERSION string."""
    if not self._cli_version:
      verbose_version = self.Run([self.cli_name, 'version', '--client'])
      match = re.search('GitVersion:"([^"]*)"', verbose_version)
      self._cli_version = match.group(1)
    return self._cli_version

  def GenerateTree(self):
    """Generates and returns the CLI tree rooted at self.cli_name."""

    # Construct the tree minus the global flags.
    tree = self.SubTree([self.cli_name])

    # Add the global flags to the root.
    text = self.Run([self.cli_name, 'options'])
    collector = _KubectlCollector(text)
    _, content = collector.Collect(strip_headings=True)
    content.append('  --help=true: List detailed command help.')
    self.AddFlags(tree, content, is_global=True)

    # Finally add the version stamps.
    tree[cli_tree.LOOKUP_CLI_VERSION] = self.GetVersion()
    tree[cli_tree.LOOKUP_VERSION] = cli_tree.VERSION

    return tree


class _ManPageCollector(object):
  """man page help document section collector.

  Attributes:
    content_indent: A string of space characters representing the indent of
      the first line of content for any section.
    heading: The heading for the next call to Collect().
    text: The list of man page lines.
  """

  def __init__(self, text):
    self.content_indent = None
    self.heading = None
    self.text = re.sub(
        u'(\u2010|\\u2010)\n *', '', encoding.Decode(text)).split('\n')

  def Collect(self):
    """Returns the heading and content lines from text."""
    content = []
    heading = self.heading
    self.heading = None
    while self.text:
      line = self.text.pop(0)
      if not heading:
        # No NAME no man page.
        if line == 'NAME':
          heading = line
        continue
      elif not line:
        pass
      elif line[0] == ' ':
        if not self.content_indent:
          self.content_indent = re.sub('[^ ].*', '', line)
        if len(line) > len(self.content_indent):
          indented_char = line[len(self.content_indent)]
          if not line.startswith(self.content_indent):
            # Subsection heading or category.
            line = '### ' + line.strip()
          elif heading == 'DESCRIPTION' and indented_char == '-':
            # Some man pages, like GNU ls(1), inline flags in DESCRIPTION.
            self.text.insert(0, line)
            self.heading = 'FLAGS'
            break
          elif heading == 'FLAGS' and indented_char not in (' ', '-'):
            self.text.insert(0, line)
            self.heading = 'DESCRIPTION'
            break
      elif line in ('SYNOPSIS', 'DESCRIPTION', 'EXIT STATUS', 'SEE ALSO'):
        self.heading = line
        break
      elif 'FLAGS' in line or 'OPTIONS' in line:
        self.heading = 'FLAGS'
        break
      elif line and line[0].isupper():
        self.heading = line.split(' ', 1)[-1]
        break
      content.append(line.rstrip())
    while content and not content[0]:
      content.pop(0)
    while content and not content[-1]:
      content.pop()
    return heading, content


class ManPageCliTreeGenerator(CliTreeGenerator):
  """man page CLI tree generator."""

  def Run(self, cmd):
    """Runs the command in cmd and returns the output as a string."""
    return subprocess.check_output(cmd)

  def GetVersion(self):
    """Returns the CLI_VERSION string."""
    return 'MAN(1)'

  def AddFlags(self, command, content, is_global=False):
    """Adds flags in content lines to command."""

    def _Add(name, description, category):
      if '=' in name:
        name, value = name.split('=', 1)
        type_ = 'string'
      else:
        value = ''
        type_ = 'bool'
      default = ''
      command[cli_tree.LOOKUP_FLAGS][name] = _Flag(
          name=name,
          description='\n'.join(description),
          type_=type_,
          value=value,
          category=category,
          default=default,
          is_required=False,
          is_global=is_global,
      )

    names = []
    description = []
    category = ''
    for line in content:
      if line.lstrip().startswith('-'):
        for name in names:
          _Add(name, description, category)
        line = line.lstrip()
        names = line.strip().replace(', -', ', --').split(', -')
        if ' ' in names[-1]:
          names[-1], text = names[-1].split(' ', 1)
          description = [text.strip()]
        else:
          description = []
      elif line.startswith('### '):
        category = line[4:]
      else:
        description.append(line)
    for name in names:
      _Add(name, description, category)

  def SubTree(self, path):
    """Generates and returns the CLI subtree rooted at path."""
    command = _Command(path)
    text = self.Run(['man'] + path)
    collector = _ManPageCollector(text)

    while True:
      heading, content = collector.Collect()
      if not heading:
        break
      elif heading == 'NAME':
        if content:
          command[cli_tree.LOOKUP_CAPSULE] = content[0].split('-', 1)[1].strip()
      elif heading == 'FLAGS':
        self.AddFlags(command, content)
      elif heading in ('DESCRIPTION', 'SEE ALSO'):
        text = _NormalizeSpace('\n'.join(content))
        if heading in command[cli_tree.LOOKUP_SECTIONS]:
          command[cli_tree.LOOKUP_SECTIONS][heading] += '\n\n' + text
        else:
          command[cli_tree.LOOKUP_SECTIONS][heading] = text
    return command

  def GenerateTree(self):
    """Generates and returns the CLI tree rooted at self.cli_name."""
    tree = self.SubTree([self.cli_name])

    # Add the version stamps.
    tree[cli_tree.LOOKUP_CLI_VERSION] = self.GetVersion()
    tree[cli_tree.LOOKUP_VERSION] = cli_tree.VERSION

    return tree


GENERATORS = {
    'bq': BqCliTreeGenerator,
    'gsutil': GsutilCliTreeGenerator,
    'kubectl': KubectlCliTreeGenerator,
}


def GetCliTreeGenerator(name):
  """Returns the CLI tree generator for name.

  Args:
    name: The CLI root command name.

  Raises:
    NoCliTreeGeneratorForCommand: if name does not have a CLI tree generator

  Returns:
    The CLI tree generator for name.
  """
  try:
    return GENERATORS[name](name)
  except KeyError:
    return ManPageCliTreeGenerator(name)


def IsCliTreeUpToDate(cli_name, tree):
  """Returns True if the CLI tree for cli_name is up to date.

  Args:
    cli_name: The CLI root command name.
    tree: The loaded CLI tree.

  Returns:
    True if the CLI tree for cli_name is up to date.
  """
  version = GetCliTreeGenerator(cli_name).GetGetVersion()
  return tree.get(cli_tree.LOOKUP_CLI_VERSION) == version


def UpdateCliTrees(cli=None, commands=None, directory=None,
                   verbose=False, warn_on_exceptions=False):
  """(re)generates the CLI trees in directory if non-existent or out ot date.

  This function uses the progress tracker because some of the updates can
  take ~minutes.

  Args:
    cli: The default CLI. If not None then the default CLI is also updated.
    commands: Update only the commands in this list.
    directory: The directory containing the CLI tree JSON files. If None
      then the default installation directories are used.
    verbose: Display a status line for up to date CLI trees if True.
    warn_on_exceptions: Emits warning messages in lieu of exceptions. Used
      during installation.

  Raises:
    NoCliTreeGeneratorForCommand: A command in commands is not supported
      (doesn't have a generator).
  """
  # Initialize the list of directories to search for CLI tree files. The default
  # CLI tree is only searched for and generated in directories[0]. Other
  # existing trees are updated in the directory in which they were found. New
  # trees are generated in directories[-1].
  directories = []
  if directory:
    directories.append(directory)
  else:
    try:
      directories.append(cli_tree.CliTreeDir())
    except cli_tree.SdkRootNotFoundError as e:
      if not warn_on_exceptions:
        raise
      log.warn(str(e))
    directories.append(cli_tree.CliTreeConfigDir())

  if not commands:
    commands = set([cli_tree.DEFAULT_CLI_NAME] + GENERATORS.keys())

  failed = []
  for command in sorted(commands):
    if command != cli_tree.DEFAULT_CLI_NAME:
      generator = GetCliTreeGenerator(command)
      try:
        generator.LoadOrGenerate(directories=directories, verbose=verbose,
                                 warn_on_exceptions=warn_on_exceptions)
      except subprocess.CalledProcessError:
        failed.append(command)
    elif cli:
      cli_tree.Load(cli=cli,
                    path=cli_tree.CliTreePath(directory=directories[0]),
                    verbose=verbose)
  if failed:
    message = 'No CLI tree {} for [{}].'.format(
        text_utils.Pluralize(len(failed), 'generator'),
        ', '.join(sorted(failed)))
    if not warn_on_exceptions:
      raise NoCliTreeGeneratorForCommand(message)
    log.warn(message)
