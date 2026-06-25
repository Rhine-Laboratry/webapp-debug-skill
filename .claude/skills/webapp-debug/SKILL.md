---
name: webapp-debug
description: コードベースとブラウザからWebアプリの機能を棚卸しし、日本語Scenario、Playwrightテスト、Google Sheetsの進捗・不具合記録を生成する。init、discover、test、full、resume、reportを明示指定した場合に使用する。
disable-model-invocation: true
argument-hint: "init|discover|test|full|resume|report [--config <path>] [--profile <name>]"
---

# Webapp Debug Claude Code Wrapper

このファイルはClaude Code用の薄いラッパーです。正準Skillとして `../../../skills/webapp-debug/SKILL.md` を読み、その指示へ `$ARGUMENTS` を渡してください。

引数がない場合、または `init`、`discover`、`test`、`full`、`resume`、`report` 以外のモードが渡された場合は、使用方法だけを返し、調査、ブラウザ起動、DB操作、Google Sheets更新を開始しません。

Claude Code固有の `disable-model-invocation`、`argument-hint`、`$ARGUMENTS` はこのラッパーだけで扱います。共通の正準Skillへ戻してはいけません。
