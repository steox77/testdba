package com.callblocker.app.ui.log

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.fragment.app.Fragment
import com.callblocker.app.data.db.AppDatabase
import com.callblocker.app.databinding.FragmentCallLogBinding
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

class CallLogFragment : Fragment() {

    private var _binding: FragmentCallLogBinding? = null
    private val binding get() = _binding!!
    private lateinit var adapter: CallLogAdapter
    private lateinit var db: AppDatabase
    private val scope = CoroutineScope(Dispatchers.IO)

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        _binding = FragmentCallLogBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        db = AppDatabase.getInstance(requireContext())

        adapter = CallLogAdapter()
        binding.recyclerView.adapter = adapter

        db.callLogDao().getAll().observe(viewLifecycleOwner) { list ->
            adapter.submitList(list)
            binding.emptyView.visibility = if (list.isEmpty()) View.VISIBLE else View.GONE
            binding.recyclerView.visibility = if (list.isEmpty()) View.GONE else View.VISIBLE
        }

        binding.btnClearLog.setOnClickListener {
            MaterialAlertDialogBuilder(requireContext())
                .setTitle("Cancella registro")
                .setMessage("Vuoi cancellare tutto il registro delle chiamate bloccate?")
                .setPositiveButton("Sì") { _, _ -> scope.launch { db.callLogDao().deleteAll() } }
                .setNegativeButton("No", null)
                .show()
        }
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }
}
