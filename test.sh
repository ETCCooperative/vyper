#!/usr/bin/env bash

valid_outputs=(bytecode ir opcodes asm)
output_selected=${valid_outputs[0]}

help()
{
  echo "Usage:"
  echo
  echo "  $0 [${valid_outputs[@]}]"
}

# Override default output_selected type with arg:1, if any
[[ ! -z $1 ]] && output_selected="${1}"

# Check that the select output type is valid.
if [[ ! " ${valid_outputs[*]} " =~ " ${output_selected}" ]]
then
  echo "Invalid output value: '$output_selected'"
  help
  exit 1
fi

set -x
python3 vyper/cli/vyper_compile.py --no-optimize --verbose -f "${output_selected}" examples/eof/test.vy 
