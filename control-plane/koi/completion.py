"""Generate local zsh completion from the command parser."""

import argparse
from pathlib import Path
import shlex


def value_action(action):
    if action.choices:
        return "(" + " ".join(str(value) for value in action.choices) + ")"
    if action.type is Path or action.dest == "guest_ssh_config":
        return "_files"
    if action.dest == "host":
        return "_ssh_hosts"
    return ""


def specs(parser):
    position = 0
    result = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        label = action.dest.replace("_", " ")
        if action.option_strings:
            help_text = (
                (action.help or "")
                .replace("[", "")
                .replace("]", "")
                .replace(":", "\\:")
            )
            for option in action.option_strings:
                spec = f"{option}[{help_text}]"
                if action.nargs != 0:
                    spec += f":{label}:{value_action(action)}"
                result.append(shlex.quote(spec))
        else:
            position += 1
            index = "*" if action.nargs == argparse.REMAINDER else str(position)
            result.append(shlex.quote(f"{index}:{label}:{value_action(action)}"))
    return " ".join(result)


def zsh(parser):
    sub = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    descriptions = {action.dest: action.help for action in sub._choices_actions}
    commands = " ".join(
        shlex.quote(f"{name}:{descriptions.get(name) or name}") for name in sub.choices
    )
    branches = "\n".join(
        f"    {name}) _arguments {specs(command)} ;;"
        for name, command in sub.choices.items()
    )
    return f"""#compdef koi
_koi() {{
  local context state state_descr line
  typeset -A opt_args
  local -a commands
  commands=({commands})
  _arguments -C {specs(parser)} '1:command:->command' '*::argument:->args'
  case $state in
    command) _describe 'command' commands ;;
    args) case $line[1] in
{branches}
    esac ;;
  esac
}}
if [[ $funcstack[1] == _koi ]]; then
  _koi "$@"
else
  compdef _koi koi
fi
"""
