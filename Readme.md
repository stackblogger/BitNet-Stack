<p align="center">
  <img src="docs/assets/bitnet-stack.png" alt="BitNet-Stack" />
</p>

<h1 align="center">BitNet-Stack</h1>

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://opensource.org/licenses/MIT)

Run a small BitNet model on local machine with one Docker command, and chat in browser.

## What you get

- **One Docker Compose command** — Build and start the LLM server with a single command. The image pulls the **official small BitNet GGUF** from Hugging Face (`BitNet-b1.58-2B-4T-gguf`, `ggml-model-i2_s.gguf`) so you do not manage weights by hand.
- **Web interface** — Open the app in your browser, start a chat, and talk to the model from a simple page.
- **Chat history in the browser** — Conversations are stored in **local storage** on your device. They stay until you clear them.
- **Clear all chats** — Use the control in the UI to remove every saved thread and stop model sessions in one go.
- **Session context** — Each chat keeps a **conversation session** with the model so follow-up messages stay in context until you start a new chat or clear data.
- **Responsive replies** — Streaming output and a tuned server path so answers show up quickly as they are generated.
- **Runs anywhere Docker runs** — On a laptop, a desktop, or a small server: clone the repo, run `docker compose`, open the URL. No extra install steps for Node or Python on the host.

## Working example

![bitnet-stack](docs/assets/conversational-chat.gif)

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (with Docker Compose)

## Quick start

### 1. Clone the repository

```bash
git clone https://github.com/stackblogger/BitNet-Stack.git
cd BitNet-Stack
```

### 2. Start the server

```bash
docker compose up --build -d
```

This builds the **LLM** image and starts one container. The model is downloaded during the image build (first time can take a while).

### 3. Open the chat UI

In your browser go to:

**http://localhost:5001**

(Port **5001** is mapped to the app inside the container on **5000**; change it in `docker-compose.yml` if you need another port.)

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
