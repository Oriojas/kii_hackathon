#include "env.h"
#include <TinyGPS++.h>
#include <WiFiClient.h>
#include <ESP8266WiFi.h>
#include <SoftwareSerial.h>
#include <ESP8266WebServer.h>
#include <ESP8266HTTPClient.h>

// Pines RX y TX del ESP8266 para puerto serie por software con el módulo GPS
const int RXPin = 13;
const int TXPin = 15;
const int GPSBaud = 9600;

// Objetos TinyGPS++, cliente WiFi y servidor web de diagnóstico local
TinyGPSPlus gps;
WiFiClient wifiClient;
ESP8266WebServer server(80);

// Puerto serie por software para el GPS
SoftwareSerial gpsSerial(RXPin, TXPin);

// Pin analógico del sensor MQ-135 (entrega señal cruda ADC, NO CO2 certificado)
const int sensorPin = A0;
int sensorValue = 0;

// Contador monotónico de secuencia por transmisión de telemetría
unsigned long telemetrySequence = 0;

String s_lat;
String s_lon;

String escapeJsonString(const char* input) {
  String out;
  if (input == nullptr) {
    return out;
  }
  for (size_t i = 0; input[i] != '\0'; ++i) {
    char c = input[i];
    if (c == '\\' || c == '"') {
      out += '\\';
    }
    out += c;
  }
  return out;
}

String buildTelemetryPayload(
  int rawSignal,
  const String& lat,
  const String& lon,
  unsigned long sequenceNumber
) {
  String payload = "{";
  payload += "\"device_id\":\"";
  payload += escapeJsonString(DEVICE_ID);
  payload += "\",";
  payload += "\"source_type\":\"physical\",";
  payload += "\"sensor_model\":\"MQ-135-raw\",";
  payload += "\"raw_signal\":";
  payload += String(rawSignal);
  payload += ",";
  payload += "\"lat\":";
  payload += lat;
  payload += ",";
  payload += "\"lon\":";
  payload += lon;
  payload += ",";
  payload += "\"sequence\":";
  payload += String(sequenceNumber);
  payload += "}";
  return payload;
}

void sendTelemetry(int rawSignal, const String& lat, const String& lon) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi disconnected. Send data OK: False");
    return;
  }

  ++telemetrySequence;
  String payload = buildTelemetryPayload(rawSignal, lat, lon, telemetrySequence);

  HTTPClient http;
  if (!http.begin(wifiClient, SERVER_URL)) {
    Serial.println("HTTP begin failed. Send data OK: False");
    return;
  }

  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + API_TOKEN);

  int httpCode = http.POST(payload);
  if (httpCode > 0) {
    Serial.print("HTTP POST code: ");
    Serial.println(httpCode);
    if (httpCode >= 200 && httpCode < 300) {
      Serial.println("Send data OK: True");
    } else {
      Serial.println("Send data OK: False");
    }
    String responseBody = http.getString();
    Serial.print("Response: ");
    Serial.println(responseBody);
  } else {
    Serial.print("HTTP POST error: ");
    Serial.println(http.errorToString(httpCode));
    Serial.println("Send data OK: False");
  }

  Serial.print("Endpoint: ");
  Serial.println(SERVER_URL);
  Serial.print("Payload: ");
  Serial.println(payload);

  http.end();
}

void setup() {
  Serial.begin(115200);

  WiFi.begin(ssid, password);
  delay(500);

  // Conexión Wi-Fi usando credenciales definidas en env.h
  while (WiFi.status() != WL_CONNECTED) {
    delay(1000);
    Serial.println("Connecting..");
  }

  Serial.println("======================================");
  Serial.print("Conectado a:\t");
  Serial.println(WiFi.SSID());
  Serial.print("IP address:\t");
  Serial.println(WiFi.localIP());
  Serial.print("Device ID:\t");
  Serial.println(DEVICE_ID);
  Serial.println("======================================");

  server.on("/", handle_OnConnect);
  server.onNotFound(handle_NotFound);

  server.begin();
  Serial.println("HTTP server started");

  gpsSerial.begin(GPSBaud);
}

void loop() {
  server.handleClient();

  while (gpsSerial.available() > 0) {
    if (gps.encode(gpsSerial.read())) {
      displayInfo();
    }
  }

  if (millis() > 5000 && gps.charsProcessed() < 10) {
    Serial.println("No GPS detected");
    while (true) {
      server.handleClient();
      delay(100);
    }
  }
}

void displayInfo() {
  sensorValue = analogRead(sensorPin);
  Serial.print("MQ-135 raw_signal (ADC): ");
  Serial.println(sensorValue);

  if (gps.location.isValid()) {
    s_lat = String(gps.location.lat(), 7);
    s_lon = String(gps.location.lng(), 7);
    Serial.print("Latitude: ");
    Serial.println(s_lat);
    Serial.print("Longitude: ");
    Serial.println(s_lon);
    Serial.print("Altitude: ");
    Serial.println(gps.altitude.meters());
  } else {
    Serial.println("Location: Not Available");
  }

  Serial.print("Date: ");
  if (gps.date.isValid()) {
    Serial.print(gps.date.month());
    Serial.print("/");
    Serial.print(gps.date.day());
    Serial.print("/");
    Serial.println(gps.date.year());
  } else {
    Serial.println("Not Available");
  }

  Serial.print("Time: ");
  if (gps.time.isValid()) {
    if (gps.time.hour() < 10) Serial.print(F("0"));
    Serial.print(gps.time.hour());
    Serial.print(":");
    if (gps.time.minute() < 10) Serial.print(F("0"));
    Serial.print(gps.time.minute());
    Serial.print(":");
    if (gps.time.second() < 10) Serial.print(F("0"));
    Serial.print(gps.time.second());
    Serial.print(".");
    if (gps.time.centisecond() < 10) Serial.print(F("0"));
    Serial.println(gps.time.centisecond());
  } else {
    Serial.println("Not Available");
  }

  if (gps.location.isValid()) {
    sendTelemetry(sensorValue, s_lat, s_lon);
    delay(10000);
  } else {
    Serial.println("Send data OK: False");
  }

  Serial.println();
  Serial.println();
  delay(5000);
}

void handle_OnConnect() {
  server.send(200, "text/plain", "OK");
}

void handle_NotFound() {
  server.send(404, "text/plain", "Not found");
}
