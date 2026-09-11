package com.thereprocase.bambubridge.viewing

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.AtomicFile
import org.json.JSONObject
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Service restart state is encrypted with a device-only key and excluded from backups. */
object MonitorStorage {
    private const val ALIAS = "bambu.bridge.monitor.v1"
    private fun file(context: Context) = AtomicFile(File(context.noBackupFilesDir, "print-monitor.enc"))
    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
        }.generateKey()
    }
    @Synchronized fun write(context: Context, value: JSONObject) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
        val encoded = cipher.doFinal(value.toString().toByteArray(Charsets.UTF_8))
        val target = file(context); val stream = target.startWrite()
        try { stream.write(cipher.iv); stream.write(encoded); target.finishWrite(stream) }
        catch (e: Exception) { target.failWrite(stream); throw e }
    }
    @Synchronized fun read(context: Context): JSONObject? {
        if (!file(context).baseFile.exists()) return null
        val bytes = file(context).readFully(); require(bytes.size in 29..65536)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply {
            init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, bytes.copyOfRange(0, 12)))
        }
        return JSONObject(String(cipher.doFinal(bytes.copyOfRange(12, bytes.size)), Charsets.UTF_8))
    }
    @Synchronized fun clear(context: Context) { file(context).delete() }
    @Synchronized fun updateTracker(context: Context, session: String, tracker: JSONObject): Boolean {
        val current = read(context) ?: return false
        if (current.optString("session") != session) return false
        write(context, current.put("tracker", tracker))
        return true
    }
    @Synchronized fun isActive(context: Context, session: String): Boolean =
        read(context)?.optString("session") == session
}
