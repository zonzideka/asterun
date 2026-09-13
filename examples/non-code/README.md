[中文](README.md) | [English](README.en.md)

# 非代码样例

本样例以会议纪要为输入，使用 fake 后端离线验证非 Git 工作区、文件输入和确定性验收。

## 准备样例

安装 Asterun 后，在源码或 sdist 根目录执行以下命令，将样例复制到临时目录。配置中的 `./inbox` 按配置文件位置解析：

```sh
ASTERUN_DEMO=$(mktemp -d)
cp -R examples/non-code/. "$ASTERUN_DEMO/"
```

## 提交并验收

分别提交文本和文件输入：

```sh
asterun --config "$ASTERUN_DEMO/config.example.json" --state-dir "$ASTERUN_DEMO/state" \
  task-submit --workspace inbox --script success \
  --text "整理会议纪要里的行动项" --idempotency-key demo-text-1
asterun --config "$ASTERUN_DEMO/config.example.json" --state-dir "$ASTERUN_DEMO/state" \
  task-submit --workspace inbox --input meeting-notes.txt --script success \
  --idempotency-key demo-file-1
```

将返回的任务 ID 填入以下命令，检查结果是否含有“行动项”：

```sh
asterun --config "$ASTERUN_DEMO/config.example.json" --state-dir "$ASTERUN_DEMO/state" \
  workflow-evaluate TASK_ID --checks '[{"kind":"contains","text":"行动项"}]'
```

fake 的成功脚本回显输入，用于检查任务与验收流程。实际整理会议内容时，按[安装说明](../../docs/install.md)启用 Agent 后端。
