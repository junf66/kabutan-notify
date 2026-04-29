# kabutan-notify

株探（kabutan.jp）の「市場ニュース＞注目」（category=9）から、タイトルに
「好悪材料」を含む当日付の記事を抽出し、Gmail で自分宛にメール送信する
GitHub Actions ワークフローです。

## 機能

- 一覧ページを最大 3 ページまで遡って当日の「好悪材料」記事を検索
- 各記事の「【好悪材料が混在】」セクション（あれば）を優先抽出
  - 末尾は `※` または `⇒⇒` 直前まで
- セクションが無い記事は本文中の銘柄ごとの開示情報部分を抽出
- 銘柄コードは `https://kabutan.jp/stock/?code=XXXX` へのリンクとしてHTMLメール化
- 該当 0 件の場合はメール送信せず正常終了（ログに「該当なし」）

## ファイル構成

```
.
├── .github/workflows/notify.yml   # GitHub Actions ワークフロー
├── script.py                      # スクレイピング & メール送信
├── requirements.txt               # Python 依存
└── README.md
```

## セットアップ

### 1. リポジトリをフォーク／クローン

このリポジトリを GitHub 上に用意してください。

### 2. Gmail アプリパスワードの発行

1. Google アカウントの [セキュリティ設定](https://myaccount.google.com/security)
   で 2 段階認証プロセスを有効化。
2. [アプリパスワード](https://myaccount.google.com/apppasswords) ページから
   「アプリ: メール」「デバイス: その他（kabutan-notify など任意）」を選択し、
   16 桁のアプリパスワードを発行。
3. 発行されたパスワードを控えておく（再表示はできません）。

### 3. GitHub Secrets を設定

リポジトリの `Settings` → `Secrets and variables` → `Actions` →
`New repository secret` から、以下 3 件を追加してください。

| Secret 名             | 内容                                       |
| --------------------- | ------------------------------------------ |
| `GMAIL_USER`          | 送信元 Gmail アドレス（例 `me@gmail.com`） |
| `GMAIL_APP_PASSWORD`  | 上記アプリパスワード（16 桁、空白なし）   |
| `MAIL_TO`             | 送信先メールアドレス                       |

### 4. ワークフローを有効化

`.github/workflows/notify.yml` がリポジトリに含まれていれば、Actions タブ
から自動的に検出されます。初回はリポジトリの設定で Actions を有効化して
ください。

## スケジュール

すべて JST 基準。GitHub Actions の cron は UTC 指定です。

| 実行タイミング (JST) | cron (UTC)       | 動作                                   |
| -------------------- | ---------------- | -------------------------------------- |
| 月〜木 20:05         | `5 11 * * 1-4`   | 通常配信。**祝日なら script 側で skip** |
| 日 13:35             | `35 4 * * 0`     | 月曜分の事前配信                       |
| 月〜金 13:35         | `35 4 * * 1-5`   | **祝日のみ実行**（script 側で `jpholiday` 判定） |

> 株探は祝日（市場休場日）には記事が 13:30 頃に配信されるため、月〜金 13:35 のジョブを
> 追加し、`jpholiday` で当日が祝日のときだけ走らせています。20:05 のジョブは祝日に
> はスキップして二重送信を避けます。

`workflow_dispatch` も登録してあるので、Actions タブから手動実行も可能です（手動実行時は祝日判定をバイパスして必ず実行）。

> **Note**: GitHub Actions の cron は混雑時に数分〜数十分遅延することが
> あります。重要な締切がある場合は余裕を持ったタイミングを設定してください。

## ローカルでの動作確認

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export GMAIL_USER='you@gmail.com'
export GMAIL_APP_PASSWORD='xxxxxxxxxxxxxxxx'
export MAIL_TO='you@example.com'
export TZ=Asia/Tokyo

python script.py
```

## メール仕様

- **件名**: `【株探・好悪材料】YYYY年M月D日分（記事数）`
- **本文**: 記事ごとに、抽出した銘柄エントリを以下の形式で列挙
  ```
  銘柄名 <コード> [市場]
  本文（要約せず原文）
  ```
  HTML パートでは `<コード>` 部分が `https://kabutan.jp/stock/?code=XXXX`
  へのリンクとしてレンダリングされます。

## 注意事項

- 株探の HTML 構造が変わると抽出ロジックが追従できなくなる可能性があります。
  その場合は `script.py` のセレクタや正規表現を調整してください。
- スクレイピング先サイトの利用規約・robots.txt に従って利用してください。
- リクエスト過多を避けるためリトライは指数バックオフ（最大 3 回）に
  抑えています。
