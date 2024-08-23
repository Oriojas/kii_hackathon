#include "env.h"
#include <TinyGPS++.h>
#include <WiFiClient.h>
#include <ESP8266WiFi.h>
#include <SoftwareSerial.h>
#include <ESP8266WebServer.h>
#include <ESP8266HTTPClient.h>

// ESP8266 RX and TX pins to use for software serial
int RXPin = 13;
int TXPin = 15;
int GPSBaud = 9600;

// End point
String serverName = "http://ec2-184-72-114-228.compute-1.amazonaws.com:8086/data_co_send_tokens/";

// Create a TinyGPS++ object and WiFi client
TinyGPSPlus gps;
WiFiClient wifiClient;
ESP8266WebServer server(80);

// Create a software serial port called "gpsSerial"
SoftwareSerial gpsSerial(RXPin, TXPin);

// Sensor pin
const int sensorPin = A0;
int sensorValue = 0;

String s_lat;
String s_lon;

void setup()
{
  // Start the Arduino hardware serial port at 9600 baud
  Serial.begin(115200);

  WiFi.begin(ssid, password);
  delay(500);

  // Wifi connection
  while (WiFi.status() != WL_CONNECTED) {
 
    delay(1000);
    Serial.println("Connecting..");
    
    }

  // Connection message
  Serial.println("======================================");
  Serial.print("Conectado a:\t");
  Serial.println(WiFi.SSID()); 
  Serial.print("IP address:\t");
  Serial.println(WiFi.localIP());
  Serial.println("======================================"); 

  server.on("/", handle_OnConnect);
  server.onNotFound(handle_NotFound);

  server.begin();
  Serial.println("HTTP server started");

  // Start the software serial port at the GPS's default baud
  gpsSerial.begin(GPSBaud);
  
}

void loop()
{
  // This sketch displays information every time a new sentence is correctly encoded.
  while (gpsSerial.available() > 0)
    if (gps.encode(gpsSerial.read()))
      displayInfo();

  // If 5000 milliseconds pass and there are no characters coming in
  // over the software serial port, show a "No GPS detected" error
  if (millis() > 5000 && gps.charsProcessed() < 10)
  {
    Serial.println("No GPS detected");
    while(true);
  }

}

void displayInfo() {
  
  sensorValue = analogRead(sensorPin);
  Serial.print("CO2: ");
  Serial.println(sensorValue);
  
  if (gps.location.isValid()) {
    Serial.print("Latitude: ");
    s_lat = String(gps.location.lat(), 7);
    Serial.println(s_lat);
    Serial.print("Longitude: ");
    s_lon = String(gps.location.lng(), 7);
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
  } else  {
    Serial.println("Not Available");
  }

  if (gps.location.isValid()) {  
    
   if (WiFi.status() == WL_CONNECTED) { 
    HTTPClient http;
    String url_get = serverName + "?co2=" + sensorValue + "&origin=sensor03" + "&token=" + token + "&lat=" + s_lat + "&lon=" + s_lon;    
    http.begin(wifiClient, url_get);
    int httpCode = http.GET();  // Realizar petición        
    if (httpCode > 0) {      
      Serial.println("Send data OK: True");
    } 
    
    Serial.println("EndPoint: ");
    Serial.print(url_get);
    http.end();
    delay(10000);       
   }   
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
