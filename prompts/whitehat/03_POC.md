## 🎯 目的

* **`security-agent/outputs/WHITEHAT_02_AUDITMAP.json`** の `audit_items[]` を再訪問し、
  `status` が **"ok" 以外** の項目だけを対象に **PoC（Proof-of-Concept）** を作成。
* PoC ファイルは **必ず `contracts/test/` ディレクトリ直下** に配置し、
  `forge test`（または Hardhat）で **テストが通る / 脆弱性が存在しないと確信できる** まで
  何度でも修正ループを行う。
* 検証結果を **`security-agent/outputs/WHITEHAT_03_POC.json`** にまとめる。

---

## 0. マインドセット

1. **疑念デフォルト** — 「再現できるまで粘る」
2. **実証主義** — テスト失敗→原因分析→修正を繰り返し、最終的に pass させる
3. **経済合理性** — 攻撃コスト vs 利益を必ず算出
4. **粘り強い検証** — テストが通らない限り “Vuln” 判定を出さない

---

## 1. 事前セットアップ

```pseudocode
LOAD spec      := security-agent/outputs/WHITEHAT_01_SPEC.json
LOAD auditMap  := security-agent/outputs/WHITEHAT_02_AUDITMAP.json
FILTER targets := audit_items WHERE status != "ok"
SETUP Foundry (forge init if needed) with contracts under ../contracts/src
```

* **テストファイル作成パス**:
  `contracts/test/<FileName>_<LineNo>_PoC.t.sol`
  例: `contracts/test/Vault_152_PoC.t.sol`
* Use `vm.startPrank(attacker)`・`forge create` 等で役割を模擬
* Cross-chainロジックはモック Bridge で再現

---

## 2. PoC 実装＆再試行ループ

```
FOR each target IN targets:
    attempts = 0
    WHILE attempts < 5:          // 上限5回 or pass
        1. 実装 / 攻撃シナリオ コードを書く → contracts/test/...t.sol
        2. forge test -vvv --match <PoCName>
        3. IF test pass & exploit triggers:  status="Vuln"; BREAK
        4. IF test pass & no exploit:        status="FP";  BREAK
        5. ELSE // test fail (compile/revert): 修正 → attempts++ → repeat
    END
END
```

> 上限 5 回でも pass しない場合は `status:"NeedsReview"` とし `next_steps` に課題を書く。

---

## 3. PoC 結果エンコード (`WHITEHAT_03_POC.json`)

同フォーマットのまま **追加フィールド**:

* `"attempts"` : 試行回数
* `"test_passed"` : `true/false`
* `"reproduced"` : `true` (脆弱性再現) / `false` (再現せず)

---

## 4. 完了チェックリスト

* [ ] PoC ファイルが **すべて `contracts/test/`** 配下に保存
* [ ] `forge test` 全体が **緑 (PASS)** で終了
* [ ] `status=="ok"` 項目は PoC スキップ済み
* [ ] `WHITEHAT_03_POC.json` JSON VALID & includes attempts/test\_passed fields

---

> \*\*Claude, 上記手順で Step 3 を実行してください。
>
> 1. `status:"ok"` の項目は PoC をスキップ、
> 2. それ以外は contracts/test/ に PoC を配置し、テストを通すか脆弱性を再現するまで修正を続け、
> 3. 最終結果を `security-agent/outputs/WHITEHAT_03_POC.json` として出力してください。
>    レスポンスには JSON オブジェクトのみを含めてください。\*\*
