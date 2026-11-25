#!/usr/bin/env bash
cd ~/amb
for t in T2 T3 T4 T5; do bash build/run-task.sh $t || { echo "STOPPED at $t"; exit 1; }; done
echo ALLDONE
