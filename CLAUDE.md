# Project

You're working on oxibeat, a tool that converts real-time data from a Viatom PC60FW oximeter into MIDI & OSC data for interactive installations.

It consists of a web-UI served by a Python server; the UI displays real-time data and allows configuring the system. The Python backend handles communication with the device and OSC/MIDI I/O.

## Information

external/viatom_pc60fw (a git submodule) contains a reference implementation on how to connect and handle data from the device.

doc/ documents the device findings, architecture, detection research and current status; start with doc/README.md.

This project exists in a workspace of other unrelated projects.

## Working

- Keep solutions simple.
- Ask clarification questions if needed.