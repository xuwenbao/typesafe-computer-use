# Manage the WindowsAgentArena disk. `make` prints these commands and starts nothing.
#
#   make waa-setup
#   make waa-image [ISO=path]
#   make waa-snapshot
#   make waa-restore
#   make waa-run TASKS="domain/id ..."
#   make waa-results
#
# `make waa-run` copies .waa/snapshot over .waa/storage once, then runs every task in that one VM.
# `scripts/waa run` does not restore the snapshot.

.DEFAULT_GOAL := help

.PHONY: help waa-setup waa-image waa-snapshot waa-restore waa-run waa-results

help:
	@printf '%s\n' \
		'make waa-setup                     install the pinned WindowsAgentArena checkout and image' \
		'make waa-image [ISO=path]          build the golden disk from the evaluation ISO' \
		'make waa-snapshot                  save the shut-down disk to .waa/snapshot' \
		'make waa-restore                   copy that snapshot back over .waa/storage' \
		'make waa-run TASKS="domain/id ..." restore the snapshot, then run those tasks once' \
		'make waa-results                   print scores from the last run'

waa-setup:
	scripts/waa setup

waa-image:
	scripts/waa prepare-image $(if $(ISO),--iso $(ISO),)

waa-snapshot:
	scripts/waa snapshot

waa-restore:
	scripts/waa restore

waa-run: waa-restore
	scripts/waa run $(TASKS)

waa-results:
	scripts/waa results
