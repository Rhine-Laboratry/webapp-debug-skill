---
name: webapp-debug
description: コードベースとブラウザからWebアプリの機能を棚卸しし、日本語Scenario、Playwrightテスト、Google Sheetsの進捗・不具合記録を生成する。init、discover、test、full、resume、reportを明示指定した場合に使用する。
---

# Webapp Debug Codex Wrapper

このファイルはCodex用の薄いラッパーです。正準Skillとして `../../../skills/webapp-debug/SKILL.md` を読み、その指示を優先してください。

引数がない場合、または `init`、`discover`、`test`、`full`、`resume`、`report` 以外のモードが渡された場合は、使用方法だけを返し、調査、ブラウザ起動、DB操作、Google Sheets更新を開始しません。

Codexの暗黙起動制御、表示名、default promptは `agents/openai.yaml` に置きます。Claude Code固有のfrontmatterキーはこのファイルへ追加しません。
