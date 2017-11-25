package com.example.valeriajara.umbrella;

import android.os.Bundle;
import android.app.Activity;
import android.widget.TextView;

public class CurrentWeatherInfo extends Activity {
    TextView cityName;
    TextView rainFall;
    APICaller apiCaller;
    String cityNameText;
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        apiCaller = new APICaller();
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_current_weather_info);
            setContentView(R.layout.activity_current_weather_info);
            cityName= findViewById(R.id.cityName);
            rainFall = findViewById(R.id.rainFall);
            cityNameText = getIntent().getStringExtra("cityName");
            cityName.setText(cityNameText);
            rainFall.setText(apiCaller.getSanMarcos());
        }
    }