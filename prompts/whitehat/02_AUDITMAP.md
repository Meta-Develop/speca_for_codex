### 🎯 目的

* **Step 1 で生成した `security-agent/outputs/WHITEHAT_01_SPEC.json`** を読み込み、把握したシステム全体像を参照しながらコードベースを一次スキャン。
* 送金・アクセス制御欠如・複雑演算など **ハイリスク行／関数** に **`@audit` コメント**を付与し、**脆弱性候補ヒートマップ** を作成する。
* 出力は **2 種**

  1. 注釈付きコード（ローカル保存想定。ファイル名・行番号を JSON で列挙）
  2. **`security-agent/outputs/WHITEHAT_02_AUDITMAP.json`**

     * `audit_items[]`：ファイル／行番号／概要／リスクカテゴリ（※**概要は必ず日本語**）
     * `summary`：重要ノードと次パス優先度

---

### 0. マインドセット

1. **疑念デフォルト** — 「バグはある前提」で読む。
2. **全体⇄局所往復** — 資金フロー全体を意識しつつ行単位を精査。
3. **部分的安全性の罠警戒** — 単独で安全でも組み合わせで危険になり得る。
4. **体系的マーキング** — 迷ったら `@audit-question` として残し、次パスで検証。

---

### 1. 事前セットアップ

```pseudocode
LOAD spec := security-agent/outputs/WHITEHAT_01_SPEC.json
DEFINE risk_keywords := [
  "transfer(", "call{value:", "delegatecall",
  "unchecked", "assembly",
  "mint(", "burn(", "upgradeTo(", "initialize"
]  // ※onlyOwner 等の特権関数は除外
SORT contract files: Entrypoints → AssetMgmt → Libs → Mocks
```

> **注意**: `onlyOwner`・`_checkRole` など「明示的な特権保護がある関数」はスキップ対象。
>
> * アクセス制御の「欠如」や「脆弱性」を検出するが、特権関数そのものは無視する。

---

### 2. コントラクト読み & `@audit` マーク付与フロー

| 手順                   | 処理                                              | 判断基準（例）                                                                      |
| -------------------- | ----------------------------------------------- | ---------------------------------------------------------------------------- |
| **A. Top-down Scan** | 各ファイルを上→下へ読み、関数・変数宣言を解析                         | - 外部/公開関数で資金移動<br>- **特権保護が不足** or 不整合<br>- 複雑 math（`mulDiv`, ループ, assembly） |
| **B. コメント挿入**        | 該当行直前に **日本語** で `// @audit <理由>` を挿入           | 例: `// @audit 外部送金後に state 更新、リエントランシー懸念`                                    |
| **C. メタ情報収集**        | ファイル名・行番号・日本語概要・リスクカテゴリを `audit_items[]` に push | カテゴリ: Reentrancy / AuthZ / Math / Upgrade / EconFlow                         |
| **D. Safe マーク**      | 安全確認できた箇所は `@audit-ok <根拠>`（日本語）を挿入             | 根拠例: `nonReentrant` で保護済み                                                    |

---

### 3. JSON 出力フォーマット (`WHITEHAT_02_AUDITMAP.json`)

```json
{
  "audit_items": [
    {
      "file": "src/Vault.sol",
      "line": 152,
      "snippet": "call{value: amount}();",
      "risk_category": "Reentrancy",
      "description": "外部送金が state 更新より先に実行されるためリエントランシーの可能性"
    }
  ],
  "summary": {
    "total_files": 34,
    "total_audit_flags": 87,
    "high_risk_hotspots": ["Vault.sol", "Bridge.sol", "UpgradeProxy.sol"],
    "next_focus": "Vault.withdraw のリエントランシー、RewardDistributor の未チェック演算"
  }
}
```

---

### 4. 実行詳細

1. **ファイル走査**:

   * AST 解析で `risk_keywords` ヒット行を優先。
   * `spec.system_architecture.critical_contracts[]` があれば最優先。
2. **コメント付与**: 実ファイルへ直接追記（Git diff 管理）。
3. **リスク分類**: 5 大分類＋必要なら checklist タグで細分。
4. **False-sense 防止**: `@audit-ok` でも `audit_items` に `status:"ok"` を記録。
5. **research\_sources**: 新たに参照したコードパス等を追記（URL不要の場合は repo/path）。

---

### 5. 完了チェックリスト

* [ ] すべてのファイルに `@audit` か `@audit-ok` コメント
* [ ] `audit_items` の `description` は **日本語**
* [ ] 特権関数 (`onlyOwner` 等) 単体はスキップされている
* [ ] `summary.high_risk_hotspots` に 3 件以上
* [ ] `WHITEHAT_02_AUDITMAP.json` 生成 & JSON VALID

---

> **Claude, 上記手順に従い Step 2 を実行してください。`WHITEHAT_01_SPEC.json` を参照し、特権関数は無視しつつ日本語でメタ情報を記述し、(1) 注釈付きコード と (2) `security-agent/outputs/WHITEHAT_02_AUDITMAP.json` を生成してください。レスポンスには最終 JSON オブジェクトのみを含めてください。**
