---
name: worktree
description: AIエージェント用のgit worktreeを作成・確認・削除する。複数AIを同時運用するときの作業ディレクトリ分離に使う。「worktreeを作って」「作業環境を用意して」「worktreeを消して」と言われたとき、または自分の作業ディレクトリがprimary worktreeで他のAIと衝突しそうなときに使用する。
---

# AIエージェント用worktree

## 目的

複数のAIを同時に動かすとき、同じ作業ディレクトリを共有するとブランチの切り替えと
作業ツリーが衝突する。エージェントごとにworktreeを分けて独立させる。

## 配置と担当

`<repo>`はリポジトリのディレクトリ名を指す。

| ディレクトリ | 担当 |
|---|---|
| `<repo>/` | 人間・IDE（primary worktree） |
| `<repo>-claude/` | Claude Code |
| `<repo>-codex/` | Codex |

**自分の担当以外のworktreeで作業しない。** primary worktreeは人間が使うため、
AIから勝手にブランチを切り替えない。

## 使い方

スクリプトは`.github`リポジトリにある。`.github`は各リポジトリと同じ親ディレクトリへ
cloneしておく。

```bash
cd <workspace>/.github
python scripts/setup-worktrees.py            # 全リポジトリで作成
python scripts/setup-worktrees.py --list     # 一覧
python scripts/setup-worktrees.py --remove   # 削除
```

主なオプション。

- `--repos SushiEricServerMod` … 対象リポジトリを限定する
- `--agents claude` … 対象エージェントを限定する
- `--base develop` … 起点ブランチを明示する。省略時はoriginの既定ブランチ
- `--no-copy` … git追跡外ディレクトリ（`run/`など）を複製しない
- `--install-skill` … このスキルを各リポジトリの`.claude/skills/`へ配置する。
  `.claude/`はgit管理対象外なので、環境ごとに各自で実行する

## 作成後の作業手順

worktreeはdetached HEADで作られる。同じブランチは1つのworktreeでしか
チェックアウトできないため、作成時点ではブランチを占有しない。

作業を始めるときは自分のworktreeへ移動し、Issueごとのブランチを作る。

```bash
cd <workspace>/<repo>-claude
git fetch origin --prune
git checkout -b feature/issue-<Issue番号> origin/<開発基準ブランチ>
```

## 注意点

- **ブランチの排他** … 同じブランチを複数のworktreeでチェックアウトできない。
  他のworktreeが使用中のブランチへ切り替えようとするとgitが拒否する。
- **git追跡外ファイル** … `run/`や`.idea/`はworktreeへ引き継がれない。
  スクリプトは`run/`だけを複製する。`.idea/`はIDE用のため複製しない。
- **ビルド成果物** … `build/`と`.gradle/`はworktreeごとに独立するため、
  初回ビルドはフルビルドになる。共有キャッシュの再取得は発生しない。
- **サーバーの同時起動** … 同じポートを使うため複数worktreeで同時に起動できない。
- **削除** … 必ず`--remove`か`git worktree remove`を使う。ディレクトリを直接消すと
  管理情報が残り`git worktree prune`が必要になる。
