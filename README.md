# 🌾 コモディティニュース自動通知Bot

世界情勢をリアルタイムに監視し、コモディティ（小麦・金・原油など）の価格に影響するニュースをDiscordに自動通知するBotです。

## 📊 システム概要

```
[Google News RSS] → [Claude Haiku で要約・重要度判定] → [Discord通知]
                           (重要度3以上のみ)
```

- **監視対象**: 小麦、金、原油、トウモロコシ、大豆、銅、コモディティ全般
- **実行頻度**: 30分ごと（GitHub Actionsで自動実行）
- **運用コスト**: 月額約300円（Claude API使用量のみ）

---

## 🚀 セットアップ手順

### 1. Discord Webhook URLを取得（5分）

1. Discordアプリを開き、自分専用サーバーを作成（左メニューの「+」ボタン）
2. 任意のテキストチャンネル（例: #news）を右クリック → 「チャンネルの編集」
3. 「連携サービス」→「ウェブフック」→「新しいウェブフック」
4. URLをコピー（あとで使います）

### 2. Anthropic APIキーを取得（5分）

1. https://console.anthropic.com/ にアクセス
2. アカウント作成・ログイン
3. 「API Keys」→「Create Key」でキー発行
4. クレジットカード登録（または$5分のプリペイド購入）
5. キーをコピー（あとで使います）

### 3. GitHubリポジトリを作成（3分）

1. https://github.com/ で新しいリポジトリを作成（Private推奨）
2. このフォルダのファイルを全部アップロード
   - `main.py`
   - `requirements.txt`
   - `seen.json`
   - `.github/workflows/news-check.yml`
   - `README.md`

### 4. GitHub Secretsに認証情報を登録（2分）

リポジトリの「Settings」→「Secrets and variables」→「Actions」→「New repository secret」

以下2つを追加：

| Name | Value |
|------|-------|
| `ANTHROPIC_API_KEY` | (手順2で取得したAPIキー) |
| `DISCORD_WEBHOOK_URL` | (手順1で取得したURL) |

### 5. 動作確認

1. リポジトリの「Actions」タブ
2. 左の「Commodity News Bot」をクリック
3. 右上の「Run workflow」で手動実行
4. Discordに通知が届けば成功！ 🎉

---

## ⚙️ カスタマイズ方法

### 監視対象の追加・変更

`main.py` の `FEEDS` 辞書を編集：

```python
FEEDS = {
    "銀": "https://news.google.com/rss/search?q=silver+price&hl=ja&gl=JP&ceid=JP:ja",
    # 追加したい項目をここに
}
```

### 通知の頻度を上げたい

`.github/workflows/news-check.yml` のcronを編集：

```yaml
- cron: '*/15 * * * *'  # 15分ごとに変更
```

※ GitHub Actions無料枠は月2000分。30分毎なら十分無料枠内で収まります。

### 通知の閾値を変更

`main.py` の `IMPORTANCE_THRESHOLD` を編集：

```python
IMPORTANCE_THRESHOLD = 4  # 4以上だけ通知（ノイズ削減）
IMPORTANCE_THRESHOLD = 2  # 2以上で通知（多めに受け取る）
```

---

## 🔍 トラブルシューティング

**Q: 通知が全く来ない**
- A: GitHub Actionsが実行されているか「Actions」タブで確認
- A: 重要度3以上のニュースが無かった可能性あり（ログを確認）

**Q: Claude APIエラーが出る**
- A: Anthropic Consoleでクレジット残高を確認
- A: APIキーが正しくSecretsに登録されているか確認

**Q: 同じニュースが何度も通知される**
- A: `seen.json` のコミットが失敗している可能性あり。Actions権限を確認

---

## 💰 コスト試算

| 項目 | 月額 |
|------|------|
| GitHub Actions | 0円（無料枠内） |
| Claude Haiku API | 約300円 |
| Discord | 0円 |
| **合計** | **約300円** |

※ Claude Haiku 4.5 = 入力$1 / 出力$5 per Mトークン
※ 1日50記事想定、1記事あたり入力500+出力150トークン

---

## 📝 ライセンス

個人利用向け。自由に改変してください。

## 🙏 Credits

- Google News RSS
- Anthropic Claude API
- GitHub Actions
- Discord Webhooks
