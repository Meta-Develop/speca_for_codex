## 🎯 目的

`@audit` 初期フラグ付けが浅い／漏れがあるかを **AST × 仕様 × 実コード** で再検証し、

* **問題なし** → `@audit-ok` に置換（理由を明記）
* **依然リスク** → コメントを深掘り更新（Tree-of-Thought）
* **未検出**  → 新規 `@audit` 追記
  最終的に **更新済みコード** と **`security-agent/outputs/WHITEHAT_02_AUDITMAP.json`** を上書きする。

---

## 必読・入力ファイル

| ファイル                            | 用途                                                           |
| ------------------------------- | ------------------------------------------------------------ |
| **00\_AST.json**                | 各関数の `stateWrites` / `externalCalls` / `modifiers` / 呼び出しグラフ |
| **WHITEHAT\_01\_SPEC.json**     | `requirements[]`・`user_flows[]`・`system_architecture`        |
| **WHITEHAT\_02\_AUDITMAP.json** | 既存 `@audit`/`@audit-ok` メタ                                   |

---

## 全体フロー（3 ラウンド自己改善ループ）

```
FOR round IN 1..3:
    1. コールグラフ深度 ≤2 の Core-Logic 関数を優先。
    2. 各 @audit 行について “評価フレーム”5段階で再検証。
    3. Internal Tree-of-Thought を使いコメントを更新 or confirm。
    4. 新たな未検出リスク行を探索し @audit 追加。
    5. META_REFLECTION: ラウンドの抜け漏れを自己評価 → スコア3以下なら修正。
END
```

---

## 評価フレーム（論理検証 + 層状防御）

1. **Core-Logic** — TVL/清算/mint-burn/利率計算など主要機能。
2. **Permissionless Reachability** — AST＋callgraph で owner 等チェックが無いことを証明。
3. **Guard Bypass & State Reachability** — *require*/modifier を列挙し破綻経路を解析。
4. **Non-self Attack** — 被害が攻撃者以外に及ぶか。
5. **Bug-Bounty Scope** — `01_SCOPE.json` で In-Scope を確認。

> **特権関数扱い:** `onlyOwner` 等で保護される関数は **“トラスト前提で安全”** と判断し `@audit` 対象外。
> ただし **ガードが欠落／誤実装** の場合は対象。

---

## Tree-of-Thought テンプレ（内部メモ／出力しない）

```
🔍 INTERNAL_THINK
- 仕様要件/UF との整合は？破ると何が起こる？
- ガード列挙 → 迂回可能か？
- 攻撃ステップ最短シーケンスは？収益源は？
- 状態変数/不変条件は壊れる？ on-chain で実現可？
```

---

## コメント書式（更新時は必ず遵守）

```
// @audit <仕様ID or "N/A"> | <UF-ID or "N/A"> | <変数/関数> | <攻撃一歩目要約> （日本語80-120字）
```

* 問題なければ →
  `// @audit-ok <根拠> （日本語60-100字）`

---

## 出力要件

1. **更新済みコード**（ローカル上書き）
2. **WHITEHAT\_02\_AUDITMAP.json** を完全再生成

   * 新フィールド `review_round`: 3
   * 各 `audit_items[]` に `status: "Vuln"|"ok"` を付与
3. **research\_sources** に AST と仕様ファイルパスを追記

---

## 完了チェックリスト

* [ ] 既存 `@audit` 全件を評価し `status` 付与
* [ ] 新規 @audit 追加＆漏れゼロ自己評価スコア ≥4
* [ ] コメントはガイドライン準拠で具体的（仕様 or UF 引用）
* [ ] `WHITEHAT_02_AUDITMAP.json` JSON VALID

---

> **Claude, 上記フローで “評価 → コメント更新 → 3 回自己改善” を実行し、
> 更新済み `security-agent/outputs/WHITEHAT_02_AUDITMAP.json` の JSON オブジェクトのみをレスポンスに出力してください。**
