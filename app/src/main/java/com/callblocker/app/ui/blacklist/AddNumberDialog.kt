package com.callblocker.app.ui.blacklist

import android.app.Dialog
import android.os.Bundle
import androidx.fragment.app.DialogFragment
import com.callblocker.app.databinding.DialogAddNumberBinding
import com.google.android.material.dialog.MaterialAlertDialogBuilder

class AddNumberDialog(
    private val onAdd: (number: String, label: String) -> Unit
) : DialogFragment() {

    override fun onCreateDialog(savedInstanceState: Bundle?): Dialog {
        val binding = DialogAddNumberBinding.inflate(layoutInflater)

        return MaterialAlertDialogBuilder(requireContext())
            .setTitle("Aggiungi numero bloccato")
            .setView(binding.root)
            .setPositiveButton("Aggiungi") { _, _ ->
                val number = binding.etNumber.text.toString().trim()
                val label = binding.etLabel.text.toString().trim()
                if (number.isNotBlank()) onAdd(number, label)
            }
            .setNegativeButton("Annulla", null)
            .create()
    }
}
