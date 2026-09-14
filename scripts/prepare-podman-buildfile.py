#!/usr/bin/env python3
"""Adapt the pinned Dockerfiles for Podman without changing product checkouts."""
import re
import sys
from pathlib import Path


def qualify(reference):
    if reference == 'scratch' or '$' in reference:
        return reference
    first = reference.split('/')[0]
    if '/' in reference and ('.' in first or ':' in first or first == 'localhost'):
        return reference
    return 'docker.io/' + (reference if '/' in reference else 'library/' + reference)


def prepare(text):
    stages = set()
    image_args = set()
    lines = text.splitlines()
    output = []
    # Add explicit globals for buildah versions without BuildKit's automatic args.
    output.extend(['ARG BUILDPLATFORM', 'ARG TARGETPLATFORM'])
    for line in lines:
        match = re.match(r'^(\s*FROM\s+(?:--\S+\s+)*)(\S+)(.*)$', line, re.I)
        if match:
            reference = match[2]
            image_args.update(a or b for a, b in re.findall(r'\$\{(\w+)\}|\$(\w+)', reference))
            if reference.lower() not in stages:
                reference = qualify(reference)
            output.append(match[1] + reference + match[3])
            stage = re.search(r'\s+AS\s+(\S+)\s*$', match[3], re.I)
            if stage:
                stages.add(stage[1].lower())
        else:
            output.append(line)
    for index, line in enumerate(output):
        arg = re.match(r'^(\s*ARG\s+)(\w+)=(\S+)\s*$', line, re.I)
        if arg and arg[2] in image_args:
            output[index] = arg[1] + arg[2] + '=' + qualify(arg[3])
    return '\n'.join(output) + '\n'

if __name__ == '__main__':
    Path(sys.argv[2]).write_text(prepare(Path(sys.argv[1]).read_text()))
