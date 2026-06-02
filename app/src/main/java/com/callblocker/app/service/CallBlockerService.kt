package com.callblocker.app.service

import android.content.SharedPreferences
import android.telecom.Call
import android.telecom.CallScreeningService
import androidx.preference.PreferenceManager
import com.callblocker.app.data.db.AppDatabase
import com.callblocker.app.data.db.CallLogEntry
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

class CallBlockerService : CallScreeningService() {

    private val scope = CoroutineScope(Dispatchers.IO)

    override fun onScreenCall(callDetails: Call.Details) {
        val prefs: SharedPreferences = PreferenceManager.getDefaultSharedPreferences(this)
        val db = AppDatabase.getInstance(this)
        val rawNumber = callDetails.handle?.schemeSpecificPart ?: ""
        val isUnknown = rawNumber.isBlank()
        val blockUnknown = prefs.getBoolean("block_unknown", false)

        scope.launch {
            val response = CallResponse.Builder()

            var shouldBlock = false
            var reason = ""

            if (isUnknown && blockUnknown) {
                shouldBlock = true
                reason = "Numero sconosciuto"
            } else if (!isUnknown) {
                val cleanNumber = rawNumber.replace("\\s".toRegex(), "")
                val blocked = db.blockedNumberDao().findByNumber(cleanNumber)
                    ?: db.blockedNumberDao().findByNumber(rawNumber)
                if (blocked != null) {
                    shouldBlock = true
                    reason = if (blocked.label.isNotBlank()) blocked.label else "Lista nera"
                }
            }

            if (shouldBlock) {
                response.setRejectCall(true)
                response.setDisallowCall(true)
                response.setSkipCallLog(false)
                response.setSkipNotification(true)
                db.callLogDao().insert(
                    CallLogEntry(
                        phoneNumber = if (isUnknown) "Numero privato" else rawNumber,
                        reason = reason
                    )
                )
            }

            respondToCall(callDetails, response.build())
        }
    }
}
