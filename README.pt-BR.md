# Integração Intelbras DVR para Home Assistant

<p align="center">
  <img src="https://raw.githubusercontent.com/fabiobrolo/intelbras_dvr/main/custom_components/intelbras_dvr/brand/logo.png" alt="Intelbras DVR" width="300"/>
</p>

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/default)
[![Version](https://img.shields.io/badge/version-0.4.0-blue.svg)](https://github.com/fabiobrolo/intelbras_dvr/releases)
[![Home Assistant](https://img.shields.io/badge/home--assistant-2024.4%2B-brightgreen.svg)](https://www.home-assistant.io)

[🇺🇸 English](README.md) | [🇧🇷 Português](README.pt-BR.md)

> ⚠️ **Aviso Legal**: Intelbras® e Dahua® são marcas registradas de seus respectivos proprietários. Esta é uma integração comunitária não oficial sem qualquer afiliação, endosso ou suporte da Intelbras S.A. ou Dahua Technology.

---

Integração personalizada de alta performance para o **Home Assistant** dedicada a DVRs e NVRs **Intelbras** (e equipamentos compatíveis baseados em Dahua) em conformidade com a especificação **API of HTTP Protocol Specification V3.35**. Comunicação 100% local através de APIs HTTP CGI (stream de eventos `local_push` + `DataUpdateCoordinator`) e fluxos de vídeo RTSP, com zero dependência de nuvem.

Desenvolvida seguindo os padrões arquiteturais modernos do Home Assistant (HA 2024.4+ / 2026.x / Python 3.14), utilizando `ConfigEntry[IntelbrasDVRData]` (`runtime_data`), dataclasses `EntityDescription` em todas as plataformas, descoberta 100% dinâmica de hardware/canais/mosaicos de vídeo, fluxos nativos de Reautenticação (`reauth`) e Reconfiguração (`reconfigure`), localização completa em Português do Brasil e Inglês, execução assíncrona não bloqueante e reprodução de gravações diretamente no Navegador de Mídia com miniaturas de pré-visualização.

---

## 🚀 Melhorias Arquiteturais Recentes (v0.4.0)

1. **Arquitetura Padrão Platinum Home Assistant:**
   * Tipagem estrita PEP 695 (`type IntelbrasDVRConfigEntry = ConfigEntry[IntelbrasDVRData]`) e estado estruturado `DVRRuntimeState(TypedDict)` no `DataUpdateCoordinator`.
   * Uso padronizado de dataclasses `EntityDescription` (`SensorEntityDescription`, `BinarySensorEntityDescription`, `ButtonEntityDescription`, `NumberEntityDescription`, `SelectEntityDescription`, `SwitchEntityDescription`, `UpdateEntityDescription`, `CameraEntityDescription`) em todas as plataformas.
   * Fluxos nativos de `reauth` e `reconfigure` no `config_flow.py`, permitindo atualizar credenciais ou alterar IP/portas diretamente pela interface do Home Assistant sem perder o histórico das entidades.
   * Tratamento assíncrono rigoroso de exceções (`ConfigEntryAuthFailed`, `ConfigEntryNotReady`, `ServiceValidationError` e `HomeAssistantError`) com suporte completo a traduções via `translation_domain` e `translation_key`.

2. **Descoberta 100% Dinâmica de Hardware e Configurações (HTTP API V3.35):**
   * Método unificado `async_discover_channels()` que consulta `ChannelTitle`, `VideoIn` (`devVideoInput.cgi?action=getCollect`) e `RemoteDevice` para detectar dinamicamente a quantidade total de canais do DVR (`4`, `8`, `16`, `32`), câmeras analógicas e IP ativas, formato de sinal (`HDCVI`, `AHD`, `TVI`, `CVBS`, `IP`), predefinições PTZ (`ptz.cgi?action=getPresets`), codecs de áudio e perfis de vídeo.
   * **Descoberta Dinâmica de Mosaico de Monitor (HDMI/VGA):** Consulta `encode.cgi?action=getCaps` (`PreviewSplitNumList`) e `split.cgi?action=getMode` para construir dinamicamente as opções traduzidas de divisão de tela (`split1`, `split4`, `split8`, `split9`, `split16`, `split25`, `split36`) e os grupos de canais de acordo com o número exato de canais do equipamento.
   * **Suporte a Atualização de Firmware V3.35:** Verifica tanto `CloudUpgrader` JSON-RPC / `upgrade.cgi?action=getInfo` quanto `upgrader.cgi?action=getState` para detecção e instalação de atualizações de firmware em nuvem.
   * **Prevenção de AutoLogout:** Define automaticamente `GUISet[0].AutoLogout=0` (V3.35 §4.5.14) e `General.AutoLogout=0` na inicialização para manter a saída de vídeo HDMI/VGA sempre ativa.
   * Zero canais, nomes de câmeras, opções de monitor ou endereços IP fixos (*hardcoded*) no código.

3. **Mecanismo Híbrido e Eficiente de Reprodução de Gravações:**
   * **Canais sem Áudio:** Transmissão direta via RTSP do DVR em HLS com 0% de uso adicional de CPU e latência mínima.
   * **Canais com Áudio (ex: Vídeo Porteiro, câmeras com microfone embutido ou áudio coaxial HDCVI):** Registro dinâmico sob demanda no `go2rtc` com transcodificação de áudio `#video=copy#audio=aac#async`. O áudio AAC é universalmente compatível com navegadores web e com o aplicativo móvel Home Assistant Companion, garantindo reprodução sem descompasso ou engasgos. O fluxo temporário é encerrado e limpo automaticamente ao final da reprodução.

4. **Localização Completa (i18n):**
   * Paridade de 100% das chaves entre `strings.json`, `translations/en.json`, `translations/pt.json` e `translations/pt-BR.json`, abrangendo títulos de entidades, estados traduzidos, templates dinâmicos do seletor de monitor, etapas de configuração/opções/reautenticação/reconfiguração, exceções e serviços.

---

## 📊 Matriz de Compatibilidade de Hardware Homologado

A tabela abaixo lista os modelos de hardware verificados, versão de firmware e o status operacional das funcionalidades:

| Modelo / Firmware | Live RTSP (Main / Sub) | Instantâneos (Snapshots) | Reprodução de Gravação (HLS) | Áudio Dinâmico AAC (go2rtc) | Miniaturas de Gravação | Eventos Push em Tempo Real | Controles de Imagem | Máscara de Privacidade | Divisão Monitor HDMI/VGA | Controles PTZ | Diagnósticos & Reinicialização |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Intelbras iMHDX 3008**<br>`4.001.00IB001.0.T` | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado |
| **Intelbras MHDX 1108**<br>`4.001.00IB000.1` | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado | ✅ Suportado |

---

## 🌟 Entidades e Plataformas Geradas

### 🖥️ Entidades do Dispositivo Principal (DVR)
| Plataforma | Entidade (`translation_key`) | Categoria | Descrição |
| :--- | :--- | :--- | :--- |
| `sensor` | `dvr_status` | Diagnóstico | Status da conexão (`online` / `offline`), versão de firmware, número de série e carimbo de data/hora do último heartbeat |
| `sensor` | `monitor_mode` | Diagnóstico | Modo ativo de divisão de tela HDMI/VGA (`split1`, `split4`, `split8`, `split9`, `split16`, etc.) com atributos `group` e `raw_value` |
| `sensor` | `disk_status` | Diagnóstico | Capacidade do disco rígido (`total_gb`, `free_gb`), percentual de uso e estado de saúde |
| `binary_sensor` | `event_stream` | Diagnóstico | Conectividade em tempo real do stream HTTP de eventos (`eventManager.cgi`) |
| `select` | `monitor_split` | Principal | Seletor gerado dinamicamente para controle da saída de monitor HDMI/VGA e visualização de canais |
| `button` | `dvr_reboot` | Configuração | Botão para reinicialização física do DVR |
| `update` | `firmware` | Configuração | Verificador de atualizações de firmware e instalação com um clique |

### 🎥 Entidades por Canal de Câmera (Modulares via Config/Options Flow)
| Plataforma | Entidade (`translation_key`) | Recurso (`feature`) | Descrição |
| :--- | :--- | :--- | :--- |
| `camera` | `main_stream` | `main_stream` | Stream RTSP de alta resolução (`subtype=0`) com fallback de snapshot CGI/FFmpeg |
| `camera` | `sub_stream` | `sub_stream` | Stream RTSP leve (`subtype=1`) ideal para painéis de parede e dashboards |
| `camera` | `snapshot` | `snapshot` | Câmera de imagem estática com intervalo de atualização configurável (`snapshot_interval`) |
| `binary_sensor` | `motion` | `motion` | Detecção de movimento em tempo real (`VideoMotion` / `SmartMotionHuman` / `SmartMotionVehicle`) |
| `binary_sensor` | `video_loss` | `motion` | Detecção de desconexão / perda de vídeo da câmera em tempo real (`VideoLoss`) |
| `binary_sensor` | `tampering` | `motion` | Detecção de obstrução / mascaramento de lente em tempo real (`VideoBlind`) |
| `select` | `color_profile` | `picture` | Perfil de ajuste de imagem ativo (`day`, `night`, `normal`) |
| `number` | `brightness`, `contrast`, `saturation`, `hue`, `sharpness` | `picture` | Controles deslizantes de imagem por perfil (`0`–`100`) |
| `button` | `reset_picture_defaults` | `picture` | Restaura o perfil de imagem atual para os padrões de fábrica (`50`) |
| `switch` | `privacy_mask` | `privacy` | Oculta o vídeo da câmera (`Brightness=0, Contrast=0, Saturation=0`) e restaura os valores anteriores ao desativar |
| `button` | `ptz_up`, `ptz_down`, `ptz_left`, `ptz_right`, `ptz_zoom_in`, `ptz_zoom_out` | `ptz` | Botões direcionais e de zoom PTZ com parada automática (criados apenas em canais com suporte a PTZ) |
| `select` | `ptz_preset` | `ptz` | Move a câmera PTZ para uma predefinição descoberta (criado apenas em canais com presets configurados) |

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

## ⚙️ Configuração, Reconfiguração e Reautenticação

1. No Home Assistant, acesse **Configurações > Dispositivos & Serviços > Adicionar Integração**.
2. Pesquise por **Intelbras DVR**.
3. **Passo 1 - Conexão (`user`):** Informe o IP/host, usuário, senha, porta HTTP (padrão `80`) e porta RTSP (padrão `554`).
4. **Passo 2 - Câmeras (`channels`):** A integração inspeciona dinamicamente todos os canais do DVR, exibe o nome personalizado e o suporte a PTZ de cada câmera, e oculta slots IP vazios. Marque as câmeras desejadas.
5. **Passo 3 - Recursos (`features`):** Selecione quais recursos habilitar (`main_stream`, `sub_stream`, `snapshot`, `motion`, `picture`, `privacy`, `ptz`) e o intervalo de cache de instantâneos (`snapshot_interval`, padrão `30` segundos).

> 💡 **Fluxo de Opções (Adicionar/Remover Câmeras e Recursos):** Clique em **Configurar** no cartão da integração a qualquer momento para ativar/desativar canais ou recursos. Canais desmarcados têm seus dispositivos órfãos removidos automaticamente do Registro de Dispositivos.
>
> 🔧 **Fluxo de Reconfiguração:** Abra o menu de três pontos no cartão da integração e selecione **Reconfigurar** para atualizar o endereço IP, portas HTTP/RTSP ou credenciais do DVR sem perder o histórico das entidades.
>
> 🔐 **Fluxo de Reautenticação:** Caso a senha do DVR seja alterada, o Home Assistant solicitará automaticamente as novas credenciais por meio de uma notificação nativa de reparo.

---

## 🛠️ Serviços Disponíveis

| Serviço | Descrição | Parâmetros |
|---|---|---|
| `intelbras_dvr.set_video_color` | Ajusta os parâmetros de cor da câmera | `channel` (`1`–`32`), `brightness`, `contrast`, `hue`, `saturation`, `acutance` (`0`–`100`), `profile` (`0`=Dia, `1`=Noite, `2`=Normal) |
| `intelbras_dvr.mask_camera` | Oculta ou restaura o vídeo da câmera | `channel` (`1`–`32`), `action` (`mask` / `unmask`) |
| `intelbras_dvr.set_monitor_split` | Altera a divisão de tela na saída HDMI/VGA | `mode` (`split1`, `split4`, `split8`, `split9`, `split16`, `split25`, `split36`), `group` (ex: `0`, `1`, `2`), `channel` (`1`–`32`) |
| `intelbras_dvr.record_camera` | Força gravação contínua manual temporária | `channel` (`1`–`32`), `duration` (`5`–`3600` segundos) |
| `intelbras_dvr.ptz_move` | Move a câmera PTZ com parada cronometrada | `channel` (`1`–`32`), `direction` (`Up`, `Down`, `Left`, `Right`, `ZoomWide`, `ZoomTele`, etc.), `speed` (`1`–`8`), `duration` (`0.1`–`10.0`s) |
| `intelbras_dvr.ptz_stop` | Interrompe o movimento PTZ em andamento | `channel` (`1`–`32`), `direction` (opcional, padrão `Up`) |
| `intelbras_dvr.ptz_preset` | Move a câmera PTZ para uma predefinição | `channel` (`1`–`32`), `preset` (`1`–`255`) |
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
