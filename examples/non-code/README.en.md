[中文](README.md) | [English](README.en.md)

# Non-code example

This example uses meeting notes and the fake backend to verify a non-Git workspace, file input, and deterministic acceptance checks offline.

## Prepare the example

After installing Asterun, run these commands from the source or sdist root to copy the example into a temporary directory. The configuration resolves `./inbox` relative to the configuration file:

```sh
ASTERUN_DEMO=$(mktemp -d)
cp -R examples/non-code/. "$ASTERUN_DEMO/"
```

## Submit and check the result

Submit a text prompt and a file input. The fixture is in Chinese; the prompt asks for action items from the meeting notes:

```sh
asterun --config "$ASTERUN_DEMO/config.example.json" --state-dir "$ASTERUN_DEMO/state" \
  task-submit --workspace inbox --script success \
  --text "整理会议纪要里的行动项" --idempotency-key demo-text-1
asterun --config "$ASTERUN_DEMO/config.example.json" --state-dir "$ASTERUN_DEMO/state" \
  task-submit --workspace inbox --input meeting-notes.txt --script success \
  --idempotency-key demo-file-1
```

Replace `TASK_ID` with a returned task ID to check whether the result contains `行动项` (action items):

```sh
asterun --config "$ASTERUN_DEMO/config.example.json" --state-dir "$ASTERUN_DEMO/state" \
  workflow-evaluate TASK_ID --checks '[{"kind":"contains","text":"行动项"}]'
```

The fake success script echoes its input, exercising the task and acceptance flow. To process the meeting content with an Agent backend, follow the [installation guide](../../docs/en/install.md).
