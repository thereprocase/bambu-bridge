package com.thereprocase.bambubridge.pairing

import com.facebook.react.ReactPackage
import com.facebook.react.bridge.NativeModule
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.uimanager.ViewManager
import com.thereprocase.bambubridge.viewing.CameraViewManager
import com.thereprocase.bambubridge.viewing.ViewingModule

class SecureBridgePackage : ReactPackage {
    override fun createNativeModules(context: ReactApplicationContext): List<NativeModule> =
        listOf(SecureBridgeModule(context), ViewingModule(context))
    override fun createViewManagers(context: ReactApplicationContext): List<ViewManager<*, *>> =
        listOf(PairedViewerManager(), CameraViewManager())
}
