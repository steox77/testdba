package com.callblocker.app

import android.app.role.RoleManager
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.telecom.TelecomManager
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.navigation.fragment.NavHostFragment
import androidx.navigation.ui.setupWithNavController
import com.callblocker.app.databinding.ActivityMainBinding
import com.google.android.material.dialog.MaterialAlertDialogBuilder

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding

    private val roleRequest = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == RESULT_OK) {
            Toast.makeText(this, "Blocco chiamate attivato!", Toast.LENGTH_SHORT).show()
        } else {
            Toast.makeText(this, "Permesso non concesso. Il blocco non funzionerà.", Toast.LENGTH_LONG).show()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val navHostFragment = supportFragmentManager
            .findFragmentById(R.id.nav_host_fragment) as NavHostFragment
        val navController = navHostFragment.navController
        binding.bottomNav.setupWithNavController(navController)

        checkAndRequestScreeningRole()
    }

    private fun checkAndRequestScreeningRole() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val roleManager = getSystemService(RoleManager::class.java)
            if (!roleManager.isRoleHeld(RoleManager.ROLE_CALL_SCREENING)) {
                MaterialAlertDialogBuilder(this)
                    .setTitle("Attiva blocco chiamate")
                    .setMessage("Per bloccare le chiamate spam, questa app deve diventare il servizio di filtraggio chiamate predefinito. Vuoi procedere?")
                    .setPositiveButton("Sì") { _, _ ->
                        val intent = roleManager.createRequestRoleIntent(RoleManager.ROLE_CALL_SCREENING)
                        roleRequest.launch(intent)
                    }
                    .setNegativeButton("Non ora", null)
                    .show()
            }
        } else {
            val telecomManager = getSystemService(TelecomManager::class.java)
            if (telecomManager.defaultDialerPackage != packageName) {
                val intent = Intent(TelecomManager.ACTION_CHANGE_DEFAULT_DIALER)
                    .putExtra(TelecomManager.EXTRA_CHANGE_DEFAULT_DIALER_PACKAGE_NAME, packageName)
                startActivity(intent)
            }
        }
    }
}
