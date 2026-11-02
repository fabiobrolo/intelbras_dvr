# Integração Intelbras DVR para Home Assistant

<p align="center">
  <img src="https://raw.githubusercontent.com/fabiobrolo/intelbras_dvr/main/custom_components/intelbras_dvr/brand/logo.png" alt="Intelbras DVR" width="300"/>
</p>

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/default)
[![Version](https://img.shields.io/badge/version-0.3.4-blue.svg)](https://github.com/fabiobrolo/intelbras_dvr/releases)
[![Home Assistant](https://img.shields.io/badge/home--assistant-2024.4%2B-brightgreen.svg)](https://www.home-assistant.io)

[🇺🇸 English](README.md) | [🇧🇷 Português](README.pt-BR.md)

> ⚠️ **Aviso Legal**: Intelbras® e Dahua® são marcas registradas de seus respectivos proprietários. Esta é uma integração comunitária não oficial sem qualquer afiliação, endosso ou suporte da Intelbras S.A. ou Dahua Technology.

---

Integração personalizada de alta performance para o **Home Assistant** dedicada a DVRs e NVRs **Intelbras** (e equipamentos compatíveis baseados em Dahua). Comunicação 100% local através de APIs HTTP CGI e fluxos de vídeo RTSP, com zero dependência de nuvem.

Desenvolvida seguindo os padrões modernos do Home Assistant (HA 2024.4+ / 2026.x / Python 3.14), utilizando `runtime_data`, fluxo de eventos por push em tempo real, localização completa em Português do Brasil e Inglês, execução assíncrona não bloqueante e reprodução de gravações diretamente no Navegador de Mídia com miniaturas de pré-visualização.

---

## 🚀 Alterações Recentes & Melhorias de Arquitetura (v0.3.4)

1. **Descoberta 100% Dinâmica de Hardware e Configurações:**
   * Consulta automática às tabelas CGI do DVR (`Encode`, `RemoteDevice`, `VideoIn`, `ChannelTitle`, `ptz.cgi?action=getPresets`) para identificar em tempo de execução canais analógicos e IP ativos, suporte real a PTZ, codecs de áudio e perfis de vídeo.
   * Descoberta automática do endereço do `go2rtc` configurado no Home Assistant para transcodificação dinâmica de áudio.
   * Zero canais, nomes de câmeras ou endereços IP fixos (*hardcoded*) no código.

2. **Mecanismo Híbrido e Eficiente de Reprodução de Gravações:**
   * **Canais sem Áudio:** Transmissão direta via RTSP do DVR em HLS com 0% de uso adicional de CPU e latência mínima.
   * **Canais com Áudio (ex: Vídeo Porteiro, câmeras com microfone embutido ou áudio coaxial HDCVI):** Registro dinâmico sob demanda no `go2rtc` com transcodificação de áudio `#video=copy#audio=aac#async`. O áudio AAC é universalmente compatível com navegadores web e com o aplicativo móvel Home Assistant Companion, garantindo reprodução sem descompasso ou engasgos. O fluxo temporário é encerrado e limpo automaticamente ao final da reprodução.

3. **Filtro Inteligente de Slots Vazios no Config Flow & Options Flow:**
   * Consulta em lote de `ChannelTitle`, `VideoIn` e `RemoteDevice`.
   * Canais IP não atribuídos (como slots padrão de fábrica com `RemoteDevice.Enable=false` e `SignalFormats=UNKNOWN`) são automaticamente detectados e excluídos das opções de configuração, evitando câmeras fantasmas na interface.
   * Caso uma câmera venha a ser associada a essa porta no futuro pelo menu do DVR, ela reaparecerá automaticamente na lista do Home Assistant.

4. **Conformidade Total com o Event Loop Assíncrono (Python 3.14):**
   * Remoção de todas as chamadas síncronas de escrita em arquivo (`open()`) do loop de eventos principal, eliminando avisos de `Detected blocking call inside the event loop`.
   * Registros de depuração direcionados ao logger nativo do Home Assistant (`_LOGGER.debug`) e acessos ao cache de miniaturas em disco estritamente executados no thread pool do executor (`async_add_executor_job`).

5. **Paridade Completa de Tradução (100%):**
   * Sincronização rigorosa entre `strings.json`, `translations/en.json`, `translations/pt.json` e `translations/pt-BR.json`, abrangendo todas as entidades, estados, seletores, opções de configuração, serviços e sensores de diagnóstico.

---

## 📊 Matriz de Compatibilidade de Hardware Homologado

A tabela abaixo lista os modelos de hardware verificados, versão de firmware e o status operacional das funcionalidades:

| Modelo / Firmware | Live RTSP (Main / Sub) | Instantâneos (Snapshots) | Reprodução de Gravação (HLS) | Áudio Dinâmico AAC (go2rtc) | Miniaturas de Gravação | Eventos Push em Tempo Real | Controles de Imagem | Máscara de Privacidade | Divisão Monitor HDMI/VGA | Controles PTZ | Diagnósticos & Reinicialização |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Intelbras iMHDX 3008**<br>`4.001.00IB001.0.T` | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado |

---

## 🌟 Visão Geral dos Recursos

### 🎥 Câmeras & Transmissões de Vídeo
* **Stream Principal:** Vídeo em alta resolução para gravação e vigilância detalhada.
* **Stream Secundário:** Vídeo leve (`sub_stream`) ideal para painéis de parede e cartões de visualização rápida.
* **Entidade de Instantâneo:** Câmera de imagem estática com intervalo de atualização configurável.
* **Navegador de Mídia:** Reprodução fluida de gravações passadas no player nativo do Home Assistant.

### ⚡ Eventos em Tempo Real (Push Stream)
* Conexão HTTP persistente que recebe notificações de movimento e alarmes com latência sub-segundo (< 100ms) sem polling repetitivo.
* Sensores binários para **Movimento**, **Perda de Vídeo**, **Mascaramento de Lente** e **Conectividade do Stream de Eventos**.

### 🎛️ Controles de Imagem & Privacidade
* Ajustes finos de imagem diretamente pelo Home Assistant.
* Interruptor de privacidade para ocultar a câmera quando estiver em casa.
* Botão para restaurar os padrões de fábrica.

### 🕹️ Controles PTZ & Predefinições
* Botões direcionais com parada automática programada.
* Seletor de predefinições para mover a câmera instantaneamente.

### 🖥️ Saída de Monitor Físico (HDMI/VGA)
* Alterne o mosaico de câmeras exibido na televisão ou monitor conectado diretamente ao DVR.

---

## 📦 Instalação

### Via HACS (Recomendado)
1. Certifique-se de que o [HACS](https://hacs.xyz/) está instalado.
2. Abra o **HACS** no Home Assistant.
3. Clique nos três pontos no canto superior direito e selecione **Repositórios personalizados**.
4. Insira a URL: `https://github.com/fabiobrolo/intelbras_dvr`
5. Selecione a categoria: **Integração**.
6. Clique em **Adicionar**, localize **Intelbras DVR** na lista e clique em **Baixar**.
7. Reinicie o Home Assistant.

### Instalação Manual
1. Copie a pasta `custom_components/intelbras_dvr/` para a pasta `/config/custom_components/` do seu Home Assistant.
2. Reinicie o Home Assistant.

---

## ⚙️ Configuração

1. No Home Assistant, acesse **Configurações > Dispositivos & Serviços > Adicionar Integração**.
2. Pesquise por **Intelbras DVR**.
3. **Passo 1 - Conexão:** Informe o IP/host, usuário, senha, porta HTTP (padrão `80`) e porta RTSP (padrão `554`).
4. **Passo 2 - Câmeras:** A integração detecta os canais ativos (ocultando slots vazios). Marque as câmeras desejadas.
5. **Passo 3 - Recursos:** Selecione quais recursos habilitar para as câmeras (Stream Principal, Sub Stream, Instantâneo, Movimento, Controles de Imagem, PTZ, etc.).

> 💡 **Reconfiguração:** Você pode alterar os canais ou recursos ativados a qualquer momento clicando em **Configurar** no cartão da integração.

---

## 🛠️ Serviços Disponíveis

| Serviço | Descrição | Parâmetros |
|---|---|---|
| `intelbras_dvr.set_video_color` | Ajusta os parâmetros de cor da câmera | `channel`, `brightness`, `contrast`, `hue`, `saturation`, `acutance`, `profile` |
| `intelbras_dvr.mask_camera` | Oculta ou restaura o vídeo da câmera | `channel`, `action` (`mask` / `unmask`) |
| `intelbras_dvr.set_monitor_split` | Altera a divisão de tela na saída HDMI/VGA | `mode` (`split1`, `split4`, `split8`, `split9`, `split16`), `group`, `channel` |
| `intelbras_dvr.record_camera` | Força gravação contínua manual temporária | `channel`, `duration` (segundos) |
| `intelbras_dvr.ptz_move` | Move a câmera PTZ com parada cronometrada | `channel`, `direction`, `speed`, `duration` |
| `intelbras_dvr.ptz_stop` | Interrompe o movimento PTZ em andamento | `channel`, `direction` |
| `intelbras_dvr.ptz_preset` | Move a câmera PTZ para uma predefinição | `channel`, `preset` |
| `intelbras_dvr.reboot` | Reinicia o hardware do DVR | `entry_id` (opcional) |

---

## 📺 Linha do Tempo e Miniaturas com o `custom:advanced-camera-card`

```yaml
type: custom:advanced-camera-card
cameras:
  - camera_entity: camera.minha_camera_dvr
    live_provider: ha # ou go2rtc
    media:
      type: folder
      folders:
        - dvr_recordings

folders:
  - id: dvr_recordings
    type: ha
    ha:
      url: /media-browser/browser/app%2Cmedia-source%3A%2F%2Fintelbras_dvr%2FALLFILES%7C<ENTRY_ID>%7C<CH>%7C<YYYYMMDD>
      path:
        - matchers:
            - type: title
          parsers:
            - type: date
              regexp: '(?<value>\d{2}:\d{2})'
              format: 'HH:mm'

timeline:
  show_recordings: true
  style: ribbon
  window_seconds: 3600
```

---

## 👨‍💻 Autor & Mantenedor

* **Fábio Brolo** ([@fabiobrolo](https://github.com/fabiobrolo))

## 📄 Licença

Distribuído sob a licença MIT. Consulte `LICENSE` para mais detalhes.
