package com.callblocker.app.ui.blacklist

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.fragment.app.Fragment
import com.callblocker.app.data.db.AppDatabase
import com.callblocker.app.data.db.BlockedNumber
import com.callblocker.app.databinding.FragmentBlacklistBinding
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

class BlacklistFragment : Fragment() {

    private var _binding: FragmentBlacklistBinding? = null
    private val binding get() = _binding!!
    private lateinit var adapter: BlacklistAdapter
    private lateinit var db: AppDatabase
    private val scope = CoroutineScope(Dispatchers.IO)

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        _binding = FragmentBlacklistBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        db = AppDatabase.getInstance(requireContext())

        adapter = BlacklistAdapter { number ->
            scope.launch { db.blockedNumberDao().delete(number) }
        }

        binding.recyclerView.adapter = adapter

        db.blockedNumberDao().getAll().observe(viewLifecycleOwner) { list ->
            adapter.submitList(list)
            binding.emptyView.visibility = if (list.isEmpty()) View.VISIBLE else View.GONE
        }

        binding.fabAdd.setOnClickListener {
            AddNumberDialog { number, label ->
                scope.launch {
                    db.blockedNumberDao().insert(BlockedNumber(phoneNumber = number, label = label))
                }
            }.show(parentFragmentManager, "AddNumber")
        }
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }
}
