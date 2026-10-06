package com.lovedeep.aivideoeditor

import android.app.Application

/** Starts the editor engine as soon as the app process starts, so it is ready by the time the screen is. */
class EditorApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Native.app = this
        Sources.init(this)
        Engine.startAsync(this)
    }
}
