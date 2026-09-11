// elegoo_cam_station.ino
//
// Replacement camera firmware for the Elegoo Smart Robot Car V4.0, board
// silkscreened ESP32-WROVER Camera-V1.5. Step 03 of the plan in CLAUDE.md:
// join the home network in station mode instead of raising an access point,
// so the host keeps its internet route and can reach a model.
//
// Written against Elegoo's own source, ESP32_CameraServer_AP_20220120, which
// is in elegoo-docs. Everything board specific below is taken from that sketch
// rather than inferred. Two facts in it cost most of a day before the source
// turned up, and both are worth stating loudly:
//
//   * The camera is CAMERA_MODEL_M5STACK_WIDE, not WROVER_KIT. Every online
//     source for this board says WROVER_KIT and every one of them is wrong.
//     Elegoo's own camera_pins.h contains WROVER_KIT, commented out.
//   * The link to the UNO is Serial2 on GPIO 33 (RX) and GPIO 4 (TX), not
//     UART0. Elegoo's sketch echoes that traffic to UART0 with Serial.print
//     for debugging, which is why watching the USB port made it look as though
//     the UNO were on UART0. It is not, and UART0 is free for debug output.
//
// A cross check that settles the camera model: the sketch uses GPIO 4 for
// Serial2 TX. Under WROVER_KIT, GPIO 4 is the Y2 data line and would collide.
// Under M5STACK_WIDE it is unused. The two facts confirm each other.
//
// It keeps the behaviour the client library depends on:
//
//   * TCP port 100, brace delimited JSON relayed to the UNO
//   * {Heartbeat} once a second, and a stop plus disconnect after three
//     missed replies, which is the dead man switch
//   * /capture, /stream, /control and /status
//
// BOARD SETTINGS, from Elegoo's Notes.txt and confirmed against the stock
// image's own partition table:
//
//   Board            ESP32 Wrover Module (Elegoo say ESP32 Dev Module)
//   Partition        Huge APP (3MB No OTA / 1MB SPIFFS)
//   Flash            4 MB
//   PSRAM            Enabled
//   Upload speed     921600

#include "esp_camera.h"
#include "esp_http_server.h"
#include "driver/i2s.h"
#include <WiFi.h>

// ---------------------------------------------------------------- settings

// Wifi credentials live in secrets.h, which is git-ignored. Copy
// secrets.h.example to secrets.h and fill it in before building.
#include "secrets.h"

// The access point is kept as a fallback. If the home network is unavailable,
// or the credentials above are wrong, the car still comes up the way it always
// has and is reachable at 192.168.4.1 without a reflash. This is the whole
// safety net for this change: do not remove it.
static const char *AP_SSID = "ELEGOO-FALLBACK";

static const uint16_t CMD_PORT = 100;
static const uint32_t STA_CONNECT_TIMEOUT_MS = 20000;

// The UNO, on Serial2. Elegoo's pins.
static const int UNO_RX = 33;
static const int UNO_TX = 4;
static const uint32_t UNO_BAUD = 9600;

// UART0 goes to the USB bridge and nothing else, so debug is free here.
static const uint32_t DEBUG_BAUD = 115200;
#define DBG(...) do { Serial.printf(__VA_ARGS__); } while (0)

// Elegoo's cadence: a beat every second, and the client is dropped after more
// than three go unanswered.
static const uint32_t HEARTBEAT_INTERVAL_MS = 1000;
static const int HEARTBEAT_MISSES_ALLOWED = 3;

// ------------------------------------------------------------------ audio
//
// A MAX98357A I2S amplifier driving a small speaker, so the robot can speak.
// Synthesis happens on the host; this end only plays 16 bit mono PCM that
// arrives as a WAV in an HTTP POST to /say.
//
// Set to 0 to build without it, which also gives GPIO13 back to the LED.
#define AUDIO_ENABLED 1

// Pins, chosen from Elegoo's schematic for this board (V1.0; check a V1.5
// against it). Almost everything is taken by the camera, the UNO link and the
// debug UART, and of what is left only two pins reach a solderable test pad:
//
//   BCLK  GPIO13  pad T14. The fastest signal, so it gets the cleanest pin.
//   LRC   GPIO0   pad T10. The boot strap pin, which is fine here: the amp's
//                 input is high impedance and cannot pull it low at reset, and
//                 the frame clock is slow enough not to mind the auto-program
//                 transistor hanging off it.
//   DOUT  GPIO14  WROVER module edge pin 13, no pad; needs a wire soldered to
//                 the castellation. Its neighbours are IO27 (pin 12, camera
//                 XCLK) and IO12 (pin 14, a strapping pin: bridging to it can
//                 stop the board booting). Alternatively set this to 1, TXD0 on
//                 pad T11, which needs no fine soldering but takes over the USB
//                 debug output: DBG goes silent and the boot ROM's messages come
//                 out of the speaker as a short buzz at power on.
//
// GPIO 16 and 17 look free and are not: on a WROVER they are the PSRAM.
#define I2S_BCLK_PIN 13
#define I2S_LRC_PIN   0
#define I2S_DOUT_PIN 14

// I2S1, not I2S0. On the original ESP32 the camera driver runs the parallel
// camera interface on I2S0, so audio has to have the other one.
static const i2s_port_t AUDIO_PORT = I2S_NUM_1;

// A few seconds of 16 kHz mono is a couple of hundred kilobytes, and this
// lives in PSRAM. The cap is there to bound a bad request, not a real one.
static const size_t AUDIO_MAX_BYTES = 1024 * 1024;
static const uint8_t AUDIO_DEFAULT_VOLUME = 70;   // percent, of full scale

// Status LED, from Elegoo's sketch. Lit while joining the network. GPIO13 is
// the audio bit clock when audio is built in, so the LED goes.
#if AUDIO_ENABLED && (I2S_BCLK_PIN == 13 || I2S_LRC_PIN == 13 || I2S_DOUT_PIN == 13)
static const int LED_PIN = -1;
#else
static const int LED_PIN = 13;
#endif

// ------------------------------------------------------- camera pin map
// CAMERA_MODEL_M5STACK_WIDE, straight out of Elegoo's camera_pins.h.

#define PWDN_GPIO_NUM  -1
#define RESET_GPIO_NUM 15
#define XCLK_GPIO_NUM  27
#define SIOD_GPIO_NUM  22
#define SIOC_GPIO_NUM  23
#define Y9_GPIO_NUM    19
#define Y8_GPIO_NUM    36
#define Y7_GPIO_NUM    18
#define Y6_GPIO_NUM    39
#define Y5_GPIO_NUM     5
#define Y4_GPIO_NUM    34
#define Y3_GPIO_NUM    35
#define Y2_GPIO_NUM    32
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM  26
#define PCLK_GPIO_NUM  21

// Elegoo run the master clock at 10 MHz, having changed it down from 20; their
// comment still shows the old value. Kept at theirs, because theirs works.
static const int XCLK_HZ = 10000000;

// ------------------------------------------------------------------ state

static WiFiServer commandServer(CMD_PORT);
static WiFiClient commandClient;
static uint32_t lastHeartbeatSent = 0;
static int heartbeatMisses = 0;
static bool heartbeatAnswered = false;

static bool cameraOk = false;
static int cameraErr = 0;
static uint32_t lastCameraTry = 0;
static int cameraTries = 0;
static const uint32_t CAMERA_RETRY_MS = 10000;

static httpd_handle_t cameraServer = NULL;
static httpd_handle_t streamServer = NULL;

// Frames are brace delimited in both directions. Accumulate to a closing
// brace, then act on the whole thing, exactly as Elegoo do.
static String fromClient;
static String fromUno;

#define PART_BOUNDARY "123456789000000000000987654321"
static const char *STREAM_CONTENT_TYPE =
    "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char *STREAM_BOUNDARY = "\r\n--" PART_BOUNDARY "\r\n";
static const char *STREAM_PART =
    "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

// ------------------------------------------------------------- http handlers

static esp_err_t capture_handler(httpd_req_t *req) {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }
  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.jpg");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  esp_err_t res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
  esp_camera_fb_return(fb);
  return res;
}

static esp_err_t stream_handler(httpd_req_t *req) {
  esp_err_t res = httpd_resp_set_type(req, STREAM_CONTENT_TYPE);
  if (res != ESP_OK) return res;
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

  char part[64];
  while (true) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) { res = ESP_FAIL; break; }
    size_t len = snprintf(part, sizeof(part), STREAM_PART, fb->len);
    res = httpd_resp_send_chunk(req, STREAM_BOUNDARY, strlen(STREAM_BOUNDARY));
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, part, len);
    if (res == ESP_OK)
      res = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len);
    esp_camera_fb_return(fb);
    if (res != ESP_OK) break;   // client went away
  }
  return res;
}

static bool query_value(httpd_req_t *req, const char *key, char *out,
                        size_t out_len) {
  size_t qlen = httpd_req_get_url_query_len(req) + 1;
  if (qlen <= 1) return false;
  char *query = (char *)malloc(qlen);
  if (!query) return false;
  bool found = false;
  if (httpd_req_get_url_query_str(req, query, qlen) == ESP_OK) {
    found = httpd_query_key_value(query, key, out, out_len) == ESP_OK;
  }
  free(query);
  return found;
}

static esp_err_t control_handler(httpd_req_t *req) {
  char var[32], val[32];
  if (!query_value(req, "var", var, sizeof(var)) ||
      !query_value(req, "val", val, sizeof(val))) {
    httpd_resp_send_404(req);
    return ESP_FAIL;
  }
  sensor_t *s = cameraOk ? esp_camera_sensor_get() : NULL;
  if (!s) { httpd_resp_send_500(req); return ESP_FAIL; }

  int v = atoi(val);
  int res = 0;
  if (!strcmp(var, "framesize")) {
    if (s->pixformat == PIXFORMAT_JPEG) res = s->set_framesize(s, (framesize_t)v);
  }
  else if (!strcmp(var, "quality"))        res = s->set_quality(s, v);
  else if (!strcmp(var, "brightness"))     res = s->set_brightness(s, v);
  else if (!strcmp(var, "contrast"))       res = s->set_contrast(s, v);
  else if (!strcmp(var, "saturation"))     res = s->set_saturation(s, v);
  else if (!strcmp(var, "gainceiling"))    res = s->set_gainceiling(s, (gainceiling_t)v);
  else if (!strcmp(var, "awb"))            res = s->set_whitebal(s, v);
  else if (!strcmp(var, "agc"))            res = s->set_gain_ctrl(s, v);
  else if (!strcmp(var, "aec"))            res = s->set_exposure_ctrl(s, v);
  else if (!strcmp(var, "hmirror"))        res = s->set_hmirror(s, v);
  else if (!strcmp(var, "vflip"))          res = s->set_vflip(s, v);
  else if (!strcmp(var, "awb_gain"))       res = s->set_awb_gain(s, v);
  else if (!strcmp(var, "agc_gain"))       res = s->set_agc_gain(s, v);
  else if (!strcmp(var, "aec_value"))      res = s->set_aec_value(s, v);
  else if (!strcmp(var, "aec2"))           res = s->set_aec2(s, v);
  else if (!strcmp(var, "ae_level"))       res = s->set_ae_level(s, v);
  else if (!strcmp(var, "dcw"))            res = s->set_dcw(s, v);
  else if (!strcmp(var, "bpc"))            res = s->set_bpc(s, v);
  else if (!strcmp(var, "wpc"))            res = s->set_wpc(s, v);
  else if (!strcmp(var, "raw_gma"))        res = s->set_raw_gma(s, v);
  else if (!strcmp(var, "lenc"))           res = s->set_lenc(s, v);
  else if (!strcmp(var, "special_effect")) res = s->set_special_effect(s, v);
  else if (!strcmp(var, "wb_mode"))        res = s->set_wb_mode(s, v);
  else res = -1;

  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  if (res < 0) { httpd_resp_send_500(req); return ESP_FAIL; }
  return httpd_resp_send(req, NULL, 0);
}

static esp_err_t status_handler(httpd_req_t *req) {
  char json[640];
  int n;
  sensor_t *s = cameraOk ? esp_camera_sensor_get() : NULL;

  // /status has to answer even when the camera does not, because it is how the
  // car is asked what address it ended up on.
  if (!s) {
    n = snprintf(json, sizeof(json),
        "{\"camera\":0,\"camera_err\":\"0x%x\",\"camera_tries\":%d,"
        "\"psram\":%u,\"sta_connected\":%u,\"sta_ip\":\"%s\","
        "\"ap_ip\":\"%s\",\"mac\":\"%s\",\"ssid\":\"%s\",\"heap\":%u}",
        cameraErr, cameraTries, psramFound() ? 1u : 0u,
        WiFi.status() == WL_CONNECTED ? 1u : 0u,
        WiFi.localIP().toString().c_str(),
        WiFi.softAPIP().toString().c_str(),
        WiFi.macAddress().c_str(), WiFi.SSID().c_str(),
        (unsigned)ESP.getFreeHeap());
  } else {
    n = snprintf(json, sizeof(json),
        "{\"camera\":1,\"framesize\":%u,\"quality\":%u,\"brightness\":%d,"
        "\"contrast\":%d,\"saturation\":%d,\"awb\":%u,\"aec\":%u,"
        "\"aec_value\":%u,\"ae_level\":%d,\"agc\":%u,\"agc_gain\":%u,"
        "\"hmirror\":%u,\"vflip\":%u,\"psram\":%u,"
        "\"sta_connected\":%u,\"sta_ip\":\"%s\",\"ap_ip\":\"%s\","
        "\"mac\":\"%s\",\"ssid\":\"%s\",\"heap\":%u,\"rssi\":%d,"
        "\"client\":%u}",
        s->status.framesize, s->status.quality, s->status.brightness,
        s->status.contrast, s->status.saturation, s->status.awb,
        s->status.aec, s->status.aec_value, s->status.ae_level,
        s->status.agc, s->status.agc_gain, s->status.hmirror, s->status.vflip,
        psramFound() ? 1u : 0u,
        WiFi.status() == WL_CONNECTED ? 1u : 0u,
        WiFi.localIP().toString().c_str(),
        WiFi.softAPIP().toString().c_str(),
        WiFi.macAddress().c_str(), WiFi.SSID().c_str(),
        (unsigned)ESP.getFreeHeap(), (int)WiFi.RSSI(),
        (commandClient && commandClient.connected()) ? 1u : 0u);
  }
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  return httpd_resp_send(req, json, n);
}

// -------------------------------------------------------------------- audio

static bool audioOk = false;

#if AUDIO_ENABLED

struct Clip {
  uint8_t *pcm;       // 16 bit little endian mono, in PSRAM, owned by holder
  size_t bytes;
  uint32_t rate;
  uint8_t volume;     // percent
};

// Holds at most ONE clip waiting behind the one playing. A new clip replaces
// a waiting one rather than queueing behind it, and the one playing always
// finishes. That is the host's policy too: a sentence is heard whole, and
// anything overtaken before it started is dropped, because narration about a
// decision two moves ago is worse than silence. Cutting the playing clip short
// instead would lose the end of every sentence, which is where the robot says
// what it is about to do.
static QueueHandle_t audioQueue = NULL;
static uint32_t audioRate = 0;

static void audioTask(void *) {
  static const size_t CHUNK = 256;          // mono samples per write
  static int16_t stereo[CHUNK * 2];
  Clip clip;
  for (;;) {
    if (xQueueReceive(audioQueue, &clip, portMAX_DELAY) != pdTRUE) continue;
    if (clip.rate != audioRate) {
      i2s_set_sample_rates(AUDIO_PORT, clip.rate);
      audioRate = clip.rate;
    }
    const int16_t *mono = (const int16_t *)clip.pcm;
    const size_t samples = clip.bytes / 2;
    for (size_t i = 0; i < samples; i += CHUNK) {
      const size_t n = (samples - i < CHUNK) ? samples - i : CHUNK;
      // The same sample to both channels, so it plays whichever channel the
      // amp's SD pin happens to select: left, right, or their average.
      for (size_t k = 0; k < n; k++) {
        const int16_t v = (int16_t)((int32_t)mono[i + k] * clip.volume / 100);
        stereo[2 * k] = v;
        stereo[2 * k + 1] = v;
      }
      size_t written = 0;
      i2s_write(AUDIO_PORT, stereo, n * 4, &written, portMAX_DELAY);
    }
    free(clip.pcm);
    // No i2s_zero_dma_buffer here: it would discard the last buffer or two,
    // clipping the end of the sentence. tx_desc_auto_clear already makes the
    // DMA send silence once the data runs out.
  }
}

// Find the format and the data in a RIFF WAV. Walks the chunks rather than
// assuming the classic 44 byte header, because writers add chunks.
static bool parseWav(const uint8_t *b, size_t n, uint32_t *rate,
                     uint16_t *channels, uint16_t *bits,
                     size_t *dataOffset, size_t *dataLen) {
  if (n < 12 || memcmp(b, "RIFF", 4) != 0 || memcmp(b + 8, "WAVE", 4) != 0) {
    return false;
  }
  bool haveFormat = false;
  size_t p = 12;
  while (p + 8 <= n) {
    const uint32_t size = b[p + 4] | (b[p + 5] << 8) | (b[p + 6] << 16) |
                          ((uint32_t)b[p + 7] << 24);
    if (memcmp(b + p, "fmt ", 4) == 0 && size >= 16 && p + 24 <= n) {
      const uint16_t tag = b[p + 8] | (b[p + 9] << 8);
      if (tag != 1) return false;                        // PCM only
      *channels = b[p + 10] | (b[p + 11] << 8);
      *rate = b[p + 12] | (b[p + 13] << 8) | (b[p + 14] << 16) |
              ((uint32_t)b[p + 15] << 24);
      *bits = b[p + 22] | (b[p + 23] << 8);
      haveFormat = true;
    } else if (memcmp(b + p, "data", 4) == 0) {
      *dataOffset = p + 8;
      // A streaming writer can leave the size as 0xFFFFFFFF; trust the body.
      *dataLen = (size > n - *dataOffset) ? n - *dataOffset : size;
      return haveFormat;
    }
    p += 8 + size + (size & 1);                          // chunks pad to even
  }
  return false;
}

static uint8_t volumeFrom(httpd_req_t *req) {
  char query[32];
  char value[8];
  if (httpd_req_get_url_query_str(req, query, sizeof(query)) == ESP_OK &&
      httpd_query_key_value(query, "vol", value, sizeof(value)) == ESP_OK) {
    const int v = atoi(value);
    return (uint8_t)(v < 0 ? 0 : (v > 100 ? 100 : v));
  }
  return AUDIO_DEFAULT_VOLUME;
}

// POST /say?vol=70 with a 16 bit mono PCM WAV body. Answers 202 as soon as the
// clip is queued, not when it has been played: this server also answers
// /capture, and a handler that waited out a four second sentence would stall
// every photograph behind it.
static esp_err_t say_handler(httpd_req_t *req) {
  if (!audioOk) {
    httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR,
                        "audio did not initialise");
    return ESP_FAIL;
  }
  const size_t len = req->content_len;
  if (len < 44 || len > AUDIO_MAX_BYTES) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST,
                        "body must be a WAV, under 1 MB");
    return ESP_FAIL;
  }
  uint8_t *buf = (uint8_t *)ps_malloc(len);
  if (!buf) {
    httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "out of memory");
    return ESP_FAIL;
  }
  size_t got = 0;
  while (got < len) {
    const int r = httpd_req_recv(req, (char *)buf + got, len - got);
    if (r == HTTPD_SOCK_ERR_TIMEOUT) continue;
    if (r <= 0) {
      free(buf);
      return ESP_FAIL;
    }
    got += r;
  }

  uint32_t rate = 0;
  uint16_t channels = 0, bits = 0;
  size_t offset = 0, dataLen = 0;
  if (!parseWav(buf, len, &rate, &channels, &bits, &offset, &dataLen) ||
      channels != 1 || bits != 16 || rate < 8000 || rate > 48000) {
    free(buf);
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST,
                        "expected a 16 bit mono PCM WAV, 8 to 48 kHz");
    return ESP_FAIL;
  }
  // Slide the samples to the front of the buffer, so they are 2 byte aligned
  // however the chunks before them were laid out.
  memmove(buf, buf + offset, dataLen);

  Clip clip = {buf, dataLen, rate, volumeFrom(req)};
  Clip overtaken;
  if (xQueueReceive(audioQueue, &overtaken, 0) == pdTRUE) {
    free(overtaken.pcm);           // never started; the new one supersedes it
  }
  if (xQueueSend(audioQueue, &clip, 0) != pdTRUE) {
    free(buf);
    httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "queue full");
    return ESP_FAIL;
  }
  httpd_resp_set_status(req, "202 Accepted");
  return httpd_resp_send(req, "queued", HTTPD_RESP_USE_STRLEN);
}

static bool startAudio() {
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
  cfg.sample_rate = 16000;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 8;
  cfg.dma_buf_len = 256;
  cfg.use_apll = false;
  cfg.tx_desc_auto_clear = true;   // silence on underrun, not a stuck buffer
  if (i2s_driver_install(AUDIO_PORT, &cfg, 0, NULL) != ESP_OK) return false;

  i2s_pin_config_t pins = {};
  // Set MCLK explicitly to "none". Zero-initialising the struct leaves it at 0,
  // which means GPIO0, and on the ESP32 the master clock can only come out of
  // GPIO 0, 1 or 3: i2s_set_pin would hand GPIO0 to MCLK and silently take it
  // away from LRC.
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = I2S_BCLK_PIN;
  pins.ws_io_num = I2S_LRC_PIN;
  pins.data_out_num = I2S_DOUT_PIN;
  pins.data_in_num = I2S_PIN_NO_CHANGE;
  if (i2s_set_pin(AUDIO_PORT, &pins) != ESP_OK) return false;
  i2s_zero_dma_buffer(AUDIO_PORT);
  audioRate = 16000;

  audioQueue = xQueueCreate(1, sizeof(Clip));
  if (!audioQueue) return false;
  // Core 1 alongside the Arduino loop, above it in priority. It spends almost
  // all its time blocked on DMA, so the command bridge is not starved.
  return xTaskCreatePinnedToCore(audioTask, "audio", 4096, NULL, 2, NULL, 1)
         == pdPASS;
}

#endif  // AUDIO_ENABLED

static void startHttp() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = 80;
  config.ctrl_port = 32768;

  httpd_uri_t capture_uri = {"/capture", HTTP_GET, capture_handler, NULL};
  httpd_uri_t control_uri = {"/control", HTTP_GET, control_handler, NULL};
  httpd_uri_t status_uri  = {"/status",  HTTP_GET, status_handler,  NULL};

  if (httpd_start(&cameraServer, &config) == ESP_OK) {
    httpd_register_uri_handler(cameraServer, &capture_uri);
    httpd_register_uri_handler(cameraServer, &control_uri);
    httpd_register_uri_handler(cameraServer, &status_uri);
#if AUDIO_ENABLED
    httpd_uri_t say_uri = {"/say", HTTP_POST, say_handler, NULL};
    httpd_register_uri_handler(cameraServer, &say_uri);
#endif
  }

  // The stream gets its own server so a client sitting on it forever cannot
  // block a capture or a control call.
  config.server_port = 81;
  config.ctrl_port = 32769;
  httpd_uri_t stream_uri = {"/stream", HTTP_GET, stream_handler, NULL};
  if (httpd_start(&streamServer, &config) == ESP_OK) {
    httpd_register_uri_handler(streamServer, &stream_uri);
  }
}

// ------------------------------------------------------------------- camera

static bool startCamera() {
  camera_config_t config = {};   // zero first: it has fields not set by name
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sscb_sda = SIOD_GPIO_NUM;
  config.pin_sscb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = XCLK_HZ;
  config.pixel_format = PIXFORMAT_JPEG;
  // LATEST, not WHEN_EMPTY. With two buffers, WHEN_EMPTY hands back the
  // OLDEST queued frame: the driver refills a buffer as soon as one is free,
  // so a frame sits in the queue ageing between captures and the first
  // /capture after the car moves shows where it used to be. On 2026-09-08 the
  // agent turned 90 degrees, was shown the previous view, announced it had
  // found its target and drove at empty floor.
  config.grab_mode = CAMERA_GRAB_LATEST;

  // Elegoo allocate at UXGA so the buffers are big enough for anything, then
  // drop the running size afterwards. Same here.
  if (psramFound()) {
    config.frame_size = FRAMESIZE_UXGA;
    config.jpeg_quality = 10;
    config.fb_count = 2;
    config.fb_location = CAMERA_FB_IN_PSRAM;
  } else {
    config.frame_size = FRAMESIZE_SVGA;
    config.jpeg_quality = 12;
    config.fb_count = 1;
    config.fb_location = CAMERA_FB_IN_DRAM;
  }

  cameraErr = esp_camera_init(&config);
  if (cameraErr != ESP_OK) {
    DBG("camera init failed: 0x%x\n", cameraErr);
    esp_camera_deinit();
    return false;
  }

  sensor_t *s = esp_camera_sensor_get();
  s->set_framesize(s, FRAMESIZE_SVGA);   // 800x600, what the stock firmware gave
  s->set_vflip(s, 0);
  s->set_hmirror(s, 0);
  DBG("camera ok, sensor PID 0x%02x\n", s->id.PID);
  return true;
}

// ------------------------------------------------------------------- setup

void setup() {
  Serial.begin(DEBUG_BAUD);                              // USB, debug only
  Serial2.begin(UNO_BAUD, SERIAL_8N1, UNO_RX, UNO_TX);   // the UNO
  if (LED_PIN >= 0) {
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, HIGH);
  }

  DBG("\n\nelegoo_cam_station\n");
  cameraOk = startCamera();
  lastCameraTry = millis();
  cameraTries = 1;

#if AUDIO_ENABLED
  // After the camera, so the camera has I2S0 before anything asks for a port.
  audioOk = startAudio();
  DBG(audioOk ? "audio ok on I2S1\n" : "audio init failed\n");
#endif

  // Both modes at once. Station for the home network, access point so a failed
  // join cannot strand the car somewhere unreachable.
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(AP_SSID, NULL, 9);
  WiFi.begin(STA_SSID, STA_PASS);
  WiFi.setSleep(false);            // latency matters more here than power

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED &&
         millis() - start < STA_CONNECT_TIMEOUT_MS) {
    delay(250);
  }
  if (WiFi.status() == WL_CONNECTED) {
    DBG("joined %s as %s\n", STA_SSID, WiFi.localIP().toString().c_str());
    if (LED_PIN >= 0) digitalWrite(LED_PIN, LOW);
  } else {
    DBG("could not join %s, access point still up at %s\n", STA_SSID,
        WiFi.softAPIP().toString().c_str());
  }

  startHttp();
  commandServer.begin();
  commandServer.setNoDelay(true);
  DBG("command socket listening on %u\n", CMD_PORT);
}

// -------------------------------------------------------------------- loop

static void dropClient(bool sendStop) {
  if (!commandClient) return;
  if (sendStop) Serial2.print("{\"N\":100}");
  commandClient.stop();
  DBG("client disconnected\n");
}

void loop() {
  // One client at a time, as the stock firmware did.
  if (!commandClient || !commandClient.connected()) {
    WiFiClient incoming = commandServer.available();
    if (incoming) {
      dropClient(false);
      commandClient = incoming;
      commandClient.setNoDelay(true);
      fromClient = "";
      heartbeatMisses = 0;
      heartbeatAnswered = false;
      lastHeartbeatSent = 0;      // beat immediately so the client can sync
      DBG("client connected\n");
    }
  }

  if (commandClient && commandClient.connected()) {
    // Client to UNO. Accumulate a brace delimited frame, drop spaces, and
    // swallow the client's heartbeat rather than passing it down: the UNO has
    // no idea what a heartbeat is, and Elegoo's firmware does not forward it.
    while (commandClient.available()) {
      char c = commandClient.read();
      if (c == '{') fromClient = "";
      if (c != ' ') fromClient += c;
      if (c == '}') {
        if (fromClient == "{Heartbeat}") {
          heartbeatAnswered = true;
        } else {
          Serial2.print(fromClient);
        }
        fromClient = "";
      }
    }

    uint32_t now = millis();
    if (now - lastHeartbeatSent >= HEARTBEAT_INTERVAL_MS) {
      commandClient.print("{Heartbeat}");
      lastHeartbeatSent = now;
      if (heartbeatAnswered) {
        heartbeatAnswered = false;
        heartbeatMisses = 0;
      } else {
        heartbeatMisses++;
      }
      if (heartbeatMisses > HEARTBEAT_MISSES_ALLOWED) {
        DBG("heartbeat missed %d times, dropping\n", heartbeatMisses);
        heartbeatMisses = 0;
        dropClient(true);
      }
    }
  }

  // UNO to client, also brace delimited, so a reply is forwarded whole.
  while (Serial2.available()) {
    char c = Serial2.read();
    fromUno += c;
    if (c == '}') {
      if (commandClient && commandClient.connected()) {
        commandClient.print(fromUno);
      }
      fromUno = "";
    }
  }

  // Keep trying the camera if it was not there at boot. The sensor is powered
  // from the car, so it can be dead at startup and alive once the switch is on.
  if (!cameraOk && millis() - lastCameraTry > CAMERA_RETRY_MS) {
    lastCameraTry = millis();
    cameraTries++;
    cameraOk = startCamera();
  }

  delay(1);
}
